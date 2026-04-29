package limiter

import (
	"context"
	_ "embed"
	"time"

	"github.com/redis/go-redis/v9"
)

//go:embed token_bucket.lua
var tokenBucketScript string

// TokenBucketLimiter implements Limiter using the Lua token bucket script.
// go-redis Script.Run handles EVALSHA + NOSCRIPT fallback automatically.
type TokenBucketLimiter struct {
	rdb    *redis.Client
	script *redis.Script
}

func NewTokenBucket(rdb *redis.Client) *TokenBucketLimiter {
	return &TokenBucketLimiter{
		rdb:    rdb,
		script: redis.NewScript(tokenBucketScript),
	}
}

// Load pre-caches the script SHA on Redis so the first request avoids an
// extra round-trip. Call once at startup before serving traffic.
func (l *TokenBucketLimiter) Load(ctx context.Context) error {
	return l.script.Load(ctx, l.rdb).Err()
}

// Check consumes one token from the bucket identified by key. limit is
// tokens per minute; burst is the maximum bucket depth.
func (l *TokenBucketLimiter) Check(ctx context.Context, key string, limit, burst int) (
	allowed bool, remaining int, retryAfterMs int, err error,
) {
	nowMs := time.Now().UnixMilli()
	res, err := l.script.Run(ctx, l.rdb, []string{key}, limit, burst, nowMs).Slice()
	if err != nil {
		return false, 0, 0, err
	}
	return res[0].(int64) == 1, int(res[1].(int64)), int(res[2].(int64)), nil
}
