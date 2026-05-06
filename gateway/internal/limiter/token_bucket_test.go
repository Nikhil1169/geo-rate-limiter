package limiter_test

import (
	"context"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

func redisClient(t *testing.T) *redis.Client {
	t.Helper()
	addr := "localhost:6379"
	if v := os.Getenv("REDIS_US_ADDR"); v != "" {
		addr = v
	}
	c := redis.NewClient(&redis.Options{Addr: addr})
	if err := c.Ping(context.Background()).Err(); err != nil {
		t.Skipf("redis unavailable at %s — run: docker-compose up redis-us", addr)
	}
	t.Cleanup(func() { _ = c.Close() })
	return c
}

func loadScript(t *testing.T) *redis.Script {
	t.Helper()
	src, err := os.ReadFile("token_bucket.lua")
	if err != nil {
		t.Fatalf("read token_bucket.lua: %v", err)
	}
	return redis.NewScript(string(src))
}

// evalBucket issues a single EVAL and unpacks the three return values.
func evalBucket(ctx context.Context, s *redis.Script, c *redis.Client, key string, limit, burst, nowMs int64) (allowed, remaining, retryAfterMs int64, err error) {
	res, err := s.Run(ctx, c, []string{key}, limit, burst, nowMs).Slice()
	if err != nil {
		return 0, 0, 0, err
	}
	return res[0].(int64), res[1].(int64), res[2].(int64), nil
}

// ── T1: single-threaded unit cases ──────────────────────────────────────────

func TestTokenBucket_Unit(t *testing.T) {
	ctx := context.Background()
	rdb := redisClient(t)
	script := loadScript(t)

	const limit = 60 // 1 token/sec
	const burst = 10

	fresh := func(suffix string) string {
		k := "test:tb:unit:" + suffix
		rdb.Del(ctx, k)
		return k
	}

	t.Run("fresh_key_first_call", func(t *testing.T) {
		key := fresh("fresh")
		got, rem, retry, err := evalBucket(ctx, script, rdb, key, limit, burst, 1_000_000)
		if err != nil {
			t.Fatal(err)
		}
		if got != 1 || rem != burst-1 || retry != 0 {
			t.Errorf("got {%d,%d,%d} want {1,%d,0}", got, rem, retry, burst-1)
		}
	})

	t.Run("drain_then_deny", func(t *testing.T) {
		key := fresh("drain")
		T := int64(2_000_000)
		for i := 0; i < burst; i++ {
			if got, _, _, err := evalBucket(ctx, script, rdb, key, limit, burst, T); err != nil || got != 1 {
				t.Fatalf("call %d: allowed=%d err=%v", i, got, err)
			}
		}
		got, rem, retry, err := evalBucket(ctx, script, rdb, key, limit, burst, T)
		if err != nil {
			t.Fatal(err)
		}
		// At limit=60/min: 1 token = 1000 ms
		if got != 0 || rem != 0 || retry != 1000 {
			t.Errorf("got {%d,%d,%d} want {0,0,1000}", got, rem, retry)
		}
	})

	t.Run("refill_after_one_second", func(t *testing.T) {
		key := fresh("refill")
		T := int64(3_000_000)
		for i := 0; i < burst; i++ {
			evalBucket(ctx, script, rdb, key, limit, burst, T) //nolint:errcheck
		}
		// +1000 ms → exactly 1 token at limit=60/min
		got, rem, _, err := evalBucket(ctx, script, rdb, key, limit, burst, T+1000)
		if err != nil {
			t.Fatal(err)
		}
		if got != 1 || rem != 0 {
			t.Errorf("got allowed=%d remaining=%d want 1,0", got, rem)
		}
	})

	t.Run("burst_cap_enforced", func(t *testing.T) {
		key := fresh("cap")
		T := int64(4_000_000)
		for i := 0; i < burst; i++ {
			evalBucket(ctx, script, rdb, key, limit, burst, T) //nolint:errcheck
		}
		// +1 hour would give 3600 tokens uncapped; cap must hold at burst
		_, rem, _, err := evalBucket(ctx, script, rdb, key, limit, burst, T+3_600_000)
		if err != nil {
			t.Fatal(err)
		}
		if rem != burst-1 {
			t.Errorf("remaining=%d want %d (burst cap, one consumed)", rem, burst-1)
		}
	})

	t.Run("backward_clock_no_drain", func(t *testing.T) {
		key := fresh("skew")
		T := int64(5_000_000)
		evalBucket(ctx, script, rdb, key, limit, burst, T) //nolint:errcheck

		storedBefore, err := rdb.HGet(ctx, key, "last_refill_ms").Int64()
		if err != nil {
			t.Fatal(err)
		}

		// now_ms < last_refill_ms: should not refill or go negative
		got, _, _, err := evalBucket(ctx, script, rdb, key, limit, burst, T-100)
		if err != nil {
			t.Fatal(err)
		}
		if got != 1 {
			t.Error("expected allowed (tokens still available after skewed call), got denied")
		}

		// last_refill_ms must not have moved backward
		storedAfter, err := rdb.HGet(ctx, key, "last_refill_ms").Int64()
		if err != nil {
			t.Fatal(err)
		}
		if storedAfter < storedBefore {
			t.Errorf("last_refill_ms moved backward: %d → %d", storedBefore, storedAfter)
		}
	})

	t.Run("ttl_refreshed_on_allow", func(t *testing.T) {
		key := fresh("ttl_allow")
		evalBucket(ctx, script, rdb, key, limit, burst, 6_000_000) //nolint:errcheck
		ttl, err := rdb.TTL(ctx, key).Result()
		if err != nil {
			t.Fatal(err)
		}
		if ttl <= 0 || ttl > 120*time.Second {
			t.Errorf("TTL after allow: %v, want (0, 120s]", ttl)
		}
	})

	t.Run("ttl_refreshed_on_deny", func(t *testing.T) {
		// F3 regression: TTL must be set even when the request is denied
		key := fresh("ttl_deny")
		T := int64(7_000_000)
		for i := 0; i <= burst; i++ { // burst+1 exhausts the bucket
			evalBucket(ctx, script, rdb, key, limit, burst, T) //nolint:errcheck
		}
		ttl, err := rdb.TTL(ctx, key).Result()
		if err != nil {
			t.Fatal(err)
		}
		if ttl <= 0 || ttl > 120*time.Second {
			t.Errorf("TTL after deny: %v, want (0, 120s]", ttl)
		}
	})

	t.Run("limit_zero_never_sentinel", func(t *testing.T) {
		key := fresh("lim0")
		T := int64(8_000_000)
		for i := 0; i < burst; i++ {
			got, _, _, err := evalBucket(ctx, script, rdb, key, 0, burst, T)
			if err != nil {
				t.Fatal(err)
			}
			if got != 1 {
				t.Fatalf("call %d with limit=0: expected allowed (initial burst), got denied", i)
			}
		}
		got, _, retry, err := evalBucket(ctx, script, rdb, key, 0, burst, T)
		if err != nil {
			t.Fatal(err)
		}
		if got != 0 || retry != -1 {
			t.Errorf("got {allowed=%d retry=%d} want {0,-1}", got, retry)
		}
	})
}

// ── T2: concurrency — the atomicity proof ───────────────────────────────────

func TestTokenBucket_Concurrency(t *testing.T) {
	ctx := context.Background()
	rdb := redisClient(t)
	script := loadScript(t)

	const (
		limit      = 600 // 10 tokens/sec
		burst      = 100
		goroutines = 50
		duration   = 5 * time.Second
	)

	key := "test:tb:concurrency"
	rdb.Del(ctx, key)

	var allowedCount int64
	done := make(chan struct{})
	time.AfterFunc(duration, func() { close(done) })

	start := time.Now()
	var wg sync.WaitGroup
	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			for {
				select {
				case <-done:
					return
				default:
				}
				now := time.Now().UnixMilli()
				got, _, _, err := evalBucket(ctx, script, rdb, key, limit, burst, now)
				if err != nil {
					return
				}
				if got == 1 {
					atomic.AddInt64(&allowedCount, 1)
				}
			}
		}()
	}
	wg.Wait()

	elapsedMs := time.Since(start).Milliseconds()
	// Upper bound: initial burst + tokens refilled during elapsed time, plus 2 for rounding
	maxAllowed := int64(burst) + elapsedMs*limit/60000 + 2
	t.Logf("allowed=%d elapsedMs=%d theoretical_max=%d", allowedCount, elapsedMs, maxAllowed)

	if allowedCount > maxAllowed {
		t.Errorf("atomicity violated: allowed %d > theoretical max %d", allowedCount, maxAllowed)
	}
	if allowedCount < int64(burst) {
		t.Errorf("too few allowed calls: %d < burst %d — script may be broken", allowedCount, burst)
	}
}

// ── T4: latency benchmark ────────────────────────────────────────────────────

func BenchmarkTokenBucket(b *testing.B) {
	ctx := context.Background()
	addr := "localhost:6379"
	if v := os.Getenv("REDIS_US_ADDR"); v != "" {
		addr = v
	}
	rdb := redis.NewClient(&redis.Options{Addr: addr})
	defer func() { _ = rdb.Close() }()
	if err := rdb.Ping(ctx).Err(); err != nil {
		b.Skipf("redis unavailable: %v", err)
	}

	src, err := os.ReadFile("token_bucket.lua")
	if err != nil {
		b.Fatal(err)
	}
	script := redis.NewScript(string(src))

	key := "bench:tb:user"
	rdb.Del(ctx, key)

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		now := time.Now().UnixMilli()
		script.Run(ctx, rdb, []string{key}, 600, 100, now) //nolint:errcheck
	}
	b.StopTimer()

	// T4 invariant: TTL is always set, even after N bench iterations
	ttl, _ := rdb.TTL(ctx, key).Result()
	if ttl <= 0 || ttl > 120*time.Second {
		b.Errorf("TTL after bench: %v, want (0, 120s]", ttl)
	}
}
