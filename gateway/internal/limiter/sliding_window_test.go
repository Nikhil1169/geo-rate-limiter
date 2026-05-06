package limiter_test

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/nikhil/geo-rate-limiter/gateway/internal/limiter"
	"github.com/redis/go-redis/v9"
)

// readSlidingWindowLua loads the Lua source so expiry tests can call EVAL
// with a custom window_ms, without waiting 60 seconds.
func readSlidingWindowLua(t *testing.T) string {
	t.Helper()
	src, err := os.ReadFile("sliding_window.lua")
	if err != nil {
		t.Fatalf("read sliding_window.lua: %v", err)
	}
	return string(src)
}

// evalSW calls the Lua script directly with a caller-supplied window_ms.
func evalSW(t *testing.T, rdb *redis.Client, key string, limit, windowMs int, nowMs int64, nonce string) (allowed bool, remaining int, retryMs int) {
	t.Helper()
	script := redis.NewScript(readSlidingWindowLua(t))
	res, err := script.Run(context.Background(), rdb, []string{key}, limit, windowMs, nowMs, nonce).Slice()
	if err != nil {
		t.Fatalf("EVAL: %v", err)
	}
	return res[0].(int64) == 1, int(res[1].(int64)), int(res[2].(int64))
}

// TestSlidingWindow_UnderLimit — requests below the limit are all allowed.
func TestSlidingWindow_UnderLimit(t *testing.T) {
	ctx := context.Background()
	rdb := redisClient(t)
	lim := limiter.NewSlidingWindow(rdb)
	if err := lim.Load(ctx); err != nil {
		t.Fatalf("Load: %v", err)
	}

	key := "test:sw:underlimit"
	rdb.Del(ctx, key)

	const limit = 5
	for i := 0; i < limit-1; i++ {
		allowed, remaining, retryMs, err := lim.Check(ctx, key, limit, 0)
		if err != nil {
			t.Fatalf("call %d: %v", i, err)
		}
		if !allowed {
			t.Errorf("call %d: want allowed, got denied (remaining=%d)", i, remaining)
		}
		if retryMs != 0 {
			t.Errorf("call %d: retryMs=%d want 0", i, retryMs)
		}
		// Lua returns limit - ZCARD_before_ZADD - 1.
		// After i calls in the set, ZCARD_before_ZADD == i, so remaining == limit-i-1.
		wantRemaining := limit - i - 1
		if remaining != wantRemaining {
			t.Errorf("call %d: remaining=%d want %d", i, remaining, wantRemaining)
		}
	}
}

// TestSlidingWindow_AtLimit — the call that fills the window is allowed; the
// next one is denied with a positive retry hint.
func TestSlidingWindow_AtLimit(t *testing.T) {
	ctx := context.Background()
	rdb := redisClient(t)
	lim := limiter.NewSlidingWindow(rdb)
	if err := lim.Load(ctx); err != nil {
		t.Fatalf("Load: %v", err)
	}

	key := "test:sw:atlimit"
	rdb.Del(ctx, key)

	const limit = 3

	// Consume all slots.
	for i := 0; i < limit; i++ {
		allowed, _, _, err := lim.Check(ctx, key, limit, 0)
		if err != nil {
			t.Fatalf("fill call %d: %v", i, err)
		}
		if !allowed {
			t.Fatalf("fill call %d: want allowed, got denied", i)
		}
	}

	// Next call must be denied.
	allowed, remaining, retryMs, err := lim.Check(ctx, key, limit, 0)
	if err != nil {
		t.Fatalf("deny call: %v", err)
	}
	if allowed {
		t.Error("want denied when window is full, got allowed")
	}
	if remaining != 0 {
		t.Errorf("remaining=%d want 0 on denial", remaining)
	}
	if retryMs <= 0 {
		t.Errorf("retryMs=%d want > 0 on denial", retryMs)
	}
}

// TestSlidingWindow_EntriesExpire — old entries are pruned once they fall
// outside the window, allowing new requests again.
// Uses a 200 ms synthetic window via direct EVAL so the test doesn't sleep 60 s.
func TestSlidingWindow_EntriesExpire(t *testing.T) {
	rdb := redisClient(t)
	key := "test:sw:expire"
	rdb.Del(context.Background(), key)

	const limit = 2
	const windowMs = 200 // 200 ms synthetic window

	nowMs := time.Now().UnixMilli()

	// Fill the window at t=0.
	ok1, _, _ := evalSW(t, rdb, key, limit, windowMs, nowMs, "n1")
	ok2, _, _ := evalSW(t, rdb, key, limit, windowMs, nowMs+1, "n2")
	if !ok1 || !ok2 {
		t.Fatal("expected both fill calls to be allowed")
	}

	// Third call within the window must be denied.
	ok3, _, _ := evalSW(t, rdb, key, limit, windowMs, nowMs+2, "n3")
	if ok3 {
		t.Fatal("expected denial when window is full")
	}

	// Advance time past the window — t0 and t0+1 entries are now expired.
	t1 := nowMs + int64(windowMs) + 10
	ok4, rem, _ := evalSW(t, rdb, key, limit, windowMs, t1, "n4")
	if !ok4 {
		t.Errorf("expected allow after window rolls; remaining=%d", rem)
	}
}

// TestSlidingWindow_NOSCRIPT — go-redis Script.Run recovers automatically
// when the server-side script cache is flushed.
func TestSlidingWindow_NOSCRIPT(t *testing.T) {
	ctx := context.Background()
	rdb := redisClient(t)
	lim := limiter.NewSlidingWindow(rdb)
	if err := lim.Load(ctx); err != nil {
		t.Fatalf("Load: %v", err)
	}

	if err := rdb.ScriptFlush(ctx).Err(); err != nil {
		t.Fatalf("SCRIPT FLUSH: %v", err)
	}

	key := "test:sw:noscript"
	rdb.Del(ctx, key)

	allowed, _, _, err := lim.Check(ctx, key, 10, 0)
	if err != nil {
		t.Fatalf("Check after SCRIPT FLUSH: %v", err)
	}
	if !allowed {
		t.Error("want allowed on first call after NOSCRIPT recovery")
	}
}
