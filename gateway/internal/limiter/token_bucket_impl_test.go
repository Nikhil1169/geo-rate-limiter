package limiter_test

import (
	"context"
	"testing"

	"github.com/nikhil/geo-rate-limiter/gateway/internal/limiter"
	"github.com/redis/go-redis/v9"
)

// TestTokenBucketLimiter_Wiring verifies the Go wrapper layer: Load, Check,
// and the type conversions from Lua return values. Algorithm correctness is
// already proven by TestTokenBucket_Unit (script-level tests).
func TestTokenBucketLimiter_Wiring(t *testing.T) {
	ctx := context.Background()
	rdb := redisClient(t) // reuses the helper in token_bucket_test.go

	lim := limiter.NewTokenBucket(rdb)
	if err := lim.Load(ctx); err != nil {
		t.Fatalf("Load: %v", err)
	}

	key := "test:tb:impl:wiring"
	rdb.Del(ctx, key)

	// First call on a fresh key must be allowed.
	allowed, remaining, retryMs, err := lim.Check(ctx, key, 60, 10)
	if err != nil {
		t.Fatalf("Check: %v", err)
	}
	if !allowed {
		t.Error("expected allowed=true on fresh key")
	}
	if remaining != 9 {
		t.Errorf("remaining=%d want 9", remaining)
	}
	if retryMs != 0 {
		t.Errorf("retryMs=%d want 0", retryMs)
	}
}

// TestTokenBucketLimiter_NOSCRIPT verifies that the wrapper recovers when
// the Redis script cache is flushed (simulating a Redis restart between
// Load() and the first Check()).
func TestTokenBucketLimiter_NOSCRIPT(t *testing.T) {
	ctx := context.Background()
	rdb := redisClient(t)

	lim := limiter.NewTokenBucket(rdb)
	if err := lim.Load(ctx); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// Flush script cache to trigger NOSCRIPT on next EVALSHA.
	if err := rdb.ScriptFlush(ctx).Err(); err != nil {
		t.Fatalf("SCRIPT FLUSH: %v", err)
	}

	key := "test:tb:impl:noscript"
	rdb.Del(ctx, key)

	// go-redis Script.Run falls back to EVAL automatically on NOSCRIPT.
	allowed, _, _, err := lim.Check(ctx, key, 60, 10)
	if err != nil {
		t.Fatalf("Check after SCRIPT FLUSH: %v", err)
	}
	if !allowed {
		t.Error("expected allowed=true on first call after NOSCRIPT recovery")
	}
}

// TestTokenBucketLimiter_RedisDown verifies that Check returns an error
// (not a panic) when Redis is unavailable.
func TestTokenBucketLimiter_RedisDown(t *testing.T) {
	ctx := context.Background()
	// Point at a port with nothing listening.
	badRdb := redis.NewClient(&redis.Options{Addr: "localhost:19999"})
	defer badRdb.Close()

	lim := limiter.NewTokenBucket(badRdb)
	_, _, _, err := lim.Check(ctx, "test:tb:impl:down", 60, 10)
	if err == nil {
		t.Error("expected error when Redis is unreachable, got nil")
	}
}
