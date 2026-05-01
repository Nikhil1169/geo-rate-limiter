package limiter

import (
	"context"
	_ "embed"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

//go:embed sliding_window.lua
var slidingWindowScript string

const windowMs = 60_000 // 60-second window, fixed per Contract 4

// SlidingWindowLogLimiter implements Limiter using a Redis sorted-set log.
// Each allowed request is recorded as a timestamped entry; expired entries
// are pruned atomically on every call so ZCARD reflects the live window.
//
// The burst parameter accepted by Check is ignored — sliding-window-log has
// no burst concept; limit is the only enforced ceiling.
type SlidingWindowLogLimiter struct {
	rdb    *redis.Client
	script *redis.Script
	seq    atomic.Uint64 // monotonic counter used to generate per-call nonces
}

func NewSlidingWindow(rdb *redis.Client) *SlidingWindowLogLimiter {
	return &SlidingWindowLogLimiter{
		rdb:    rdb,
		script: redis.NewScript(slidingWindowScript),
	}
}

// Load pre-caches the script SHA so the first request avoids an extra
// round-trip. Call once at startup before serving traffic.
func (l *SlidingWindowLogLimiter) Load(ctx context.Context) error {
	return l.script.Load(ctx, l.rdb).Err()
}

// Check evaluates the sliding window for the given key.
// limit is max requests per 60-second window; burst is accepted for interface
// compatibility but ignored.
func (l *SlidingWindowLogLimiter) Check(ctx context.Context, key string, limit, _ int) (
	allowed bool, remaining int, retryAfterMs int, err error,
) {
	nowMs := time.Now().UnixMilli()
	nonce := fmt.Sprintf("%d", l.seq.Add(1))

	res, err := l.script.Run(ctx, l.rdb, []string{key}, limit, windowMs, nowMs, nonce).Slice()
	if err != nil {
		return false, 0, 0, err
	}
	return res[0].(int64) == 1, int(res[1].(int64)), int(res[2].(int64)), nil
}
