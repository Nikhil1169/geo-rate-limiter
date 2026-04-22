package limiter

import "context"

// Limiter makes a rate-limit decision for an arbitrary Redis key.
// Key construction (rl:local:{region}:{tier}:{user_id}) is the caller's
// responsibility — keeping this interface key-agnostic lets the sync service
// reuse it for rl:global:* keys in Phase 3 without an interface change.
type Limiter interface {
	Check(ctx context.Context, key string, limit, burst int) (
		allowed bool,
		remaining int,
		retryAfterMs int,
		err error,
	)
}
