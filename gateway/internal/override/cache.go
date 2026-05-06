// Package override implements a short-lived in-memory cache for per-user
// rate-limit overrides stored in Redis under override:{user_id}.
// It prevents a Redis GET on every /check request while keeping overrides
// visible within ~5 seconds of being written.
package override

import (
	"context"
	"encoding/json"
	"log"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

// redisEntry is the JSON stored at override:{user_id}.
// Only limit_per_minute is used by the gateway; other fields are informational.
type redisEntry struct {
	LimitPerMinute int    `json:"limit_per_minute"`
	TTL            int    `json:"ttl"`
	Reason         string `json:"reason"`
}

// cacheEntry is one slot in the in-memory map.
type cacheEntry struct {
	limit     int       // 0 means "present but no limit set" — check hasValue
	hasValue  bool      // true = Redis key exists; false = negative cache
	expiresAt time.Time // wall-clock expiry for this cache slot
}

// inflight tracks an in-progress Redis lookup so concurrent requests for the
// same user_id share one round-trip instead of spawning N parallel GETs.
type inflight struct {
	done chan struct{} // closed when the lookup completes
	res  cacheEntry   // populated before done is closed
}

// redisGetter is the only Redis operation Cache requires.
type redisGetter interface {
	Get(ctx context.Context, key string) *redis.StringCmd
}

// Cache is a thread-safe, TTL-bounded in-memory cache for per-user overrides.
// Zero value is unusable; construct with New.
type Cache struct {
	rdb redisGetter
	ttl time.Duration

	mu       sync.RWMutex
	entries  map[string]cacheEntry
	inflight map[string]*inflight
}

// New returns a Cache backed by rdb. ttl controls how long each entry
// (including negative cache entries for absent keys) is valid.
func New(rdb redisGetter, ttl time.Duration) *Cache {
	return &Cache{
		rdb:      rdb,
		ttl:      ttl,
		entries:  make(map[string]cacheEntry),
		inflight: make(map[string]*inflight),
	}
}

// Lookup returns the override limit for userID and whether one exists.
// On a cache hit (positive or negative) it returns immediately without
// touching Redis. On a miss it does one GET per user per TTL window;
// concurrent callers for the same userID wait for the first caller's result
// rather than issuing parallel GETs.
//
// On Redis error, returns (0, false) — the caller proceeds with the tier
// policy and the error is logged.
func (c *Cache) Lookup(ctx context.Context, userID string) (limit int, hasOverride bool) {
	// Fast path: cached and not expired.
	c.mu.RLock()
	if e, ok := c.entries[userID]; ok && time.Now().Before(e.expiresAt) {
		c.mu.RUnlock()
		return e.limit, e.hasValue
	}
	c.mu.RUnlock()

	// Slow path: miss or expired. Coordinate with any in-flight lookup.
	c.mu.Lock()
	// Double-check after acquiring write lock.
	if e, ok := c.entries[userID]; ok && time.Now().Before(e.expiresAt) {
		c.mu.Unlock()
		return e.limit, e.hasValue
	}

	// Join an existing in-flight lookup if one is running.
	if fl, ok := c.inflight[userID]; ok {
		c.mu.Unlock()
		<-fl.done
		return fl.res.limit, fl.res.hasValue
	}

	// We are the first goroutine to miss — start the lookup.
	fl := &inflight{done: make(chan struct{})}
	c.inflight[userID] = fl
	c.mu.Unlock()

	entry := c.fetch(ctx, userID)

	// Publish result and remove in-flight marker.
	c.mu.Lock()
	c.entries[userID] = entry
	fl.res = entry
	delete(c.inflight, userID)
	c.mu.Unlock()

	close(fl.done)
	return entry.limit, entry.hasValue
}

// fetch does the actual Redis GET. Always returns a cacheEntry ready to store.
func (c *Cache) fetch(ctx context.Context, userID string) cacheEntry {
	tctx, cancel := context.WithTimeout(ctx, 500*time.Millisecond)
	defer cancel()

	raw, err := c.rdb.Get(tctx, "override:"+userID).Result()
	if err == redis.Nil {
		// Key doesn't exist — cache the absence.
		return cacheEntry{hasValue: false, expiresAt: time.Now().Add(c.ttl)}
	}
	if err != nil {
		log.Printf("[override] GET override:%s: %v — skipping override", userID, err)
		// Don't cache errors: next request will retry Redis immediately.
		return cacheEntry{hasValue: false, expiresAt: time.Now()} // expires instantly
	}

	var re redisEntry
	if err := json.Unmarshal([]byte(raw), &re); err != nil {
		log.Printf("[override] parse override:%s: %v — skipping override", userID, err)
		return cacheEntry{hasValue: false, expiresAt: time.Now().Add(c.ttl)}
	}
	if re.LimitPerMinute <= 0 {
		log.Printf("[override] override:%s: limit_per_minute=%d invalid — skipping", userID, re.LimitPerMinute)
		return cacheEntry{hasValue: false, expiresAt: time.Now().Add(c.ttl)}
	}

	return cacheEntry{
		limit:     re.LimitPerMinute,
		hasValue:  true,
		expiresAt: time.Now().Add(c.ttl),
	}
}
