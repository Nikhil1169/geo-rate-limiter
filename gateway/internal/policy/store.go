package policy

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nikhil/geo-rate-limiter/gateway/internal/metrics"
	"github.com/redis/go-redis/v9"
)

// redisGetter is the only Redis operation Store requires.
// Using a narrow interface keeps the store testable without implementing the
// full redis.Cmdable (which has hundreds of methods).
type redisGetter interface {
	Get(ctx context.Context, key string) *redis.StringCmd
}

// contract4 is the wire format for Redis policy keys (Contract 4).
// Unmarshal into this, validate, then convert to Policy.
type contract4 struct {
	PolicyID       string `json:"policy_id"`
	Region         string `json:"region"`
	Tier           string `json:"tier"`
	LimitPerMinute int    `json:"limit_per_minute"`
	Burst          int    `json:"burst"`
	Algorithm      string `json:"algorithm"`
	TTLSeconds     int    `json:"ttl_seconds"`
	Reason         string `json:"reason"`
	CreatedAt      string `json:"created_at"`
}

var validAlgorithms = map[string]bool{
	"token_bucket":   true,
	"sliding_window": true,
}

// Store holds the live policy cache for one region.
// It polls Redis every 5 seconds and falls back to static defaults on any error.
type Store struct {
	rdb      redisGetter
	region   string
	interval time.Duration
	tiers    []string

	mu    sync.RWMutex
	cache map[string]Policy // tier → live policy
}

// NewStore creates a Store pre-populated with static defaults.
// Call Start to begin background polling.
func NewStore(rdb redisGetter, region string) *Store {
	s := &Store{
		rdb:      rdb,
		region:   region,
		interval: 5 * time.Second,
		tiers:    []string{"free", "premium", "internal"},
		cache:    make(map[string]Policy),
	}
	// Seed with static defaults so Get never blocks on an empty cache.
	for _, tier := range s.tiers {
		if p, err := Lookup(tier); err == nil {
			s.cache[tier] = p
		}
	}
	return s
}

// Start performs an initial synchronous refresh then launches the background
// polling goroutine. The goroutine exits when ctx is cancelled.
func (s *Store) Start(ctx context.Context) {
	s.refresh(ctx)
	go func() {
		ticker := time.NewTicker(s.interval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				s.refresh(ctx)
			case <-ctx.Done():
				return
			}
		}
	}()
}

// RefreshOnce runs a single synchronous refresh cycle. Intended for tests;
// production code should call Start instead.
func (s *Store) RefreshOnce(ctx context.Context) { s.refresh(ctx) }

// Get returns the live policy for the given tier.
// Falls back to the static default (already in cache from NewStore) if Redis
// has never supplied a valid policy for this tier.
func (s *Store) Get(tier string) (Policy, error) {
	s.mu.RLock()
	p, ok := s.cache[tier]
	s.mu.RUnlock()
	if ok {
		return p, nil
	}
	// Tier not in static defaults either — unknown tier.
	return Policy{}, fmt.Errorf("unknown tier %q", tier)
}

// refresh reads policy:{region}:{tier} from Redis for all tiers.
// On success+valid JSON: updates cache and bumps the policy-version metric.
// On any failure: falls back to the static default for that tier.
func (s *Store) refresh(ctx context.Context) {
	tctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()

	s.mu.Lock()
	defer s.mu.Unlock()

	for _, tier := range s.tiers {
		redisKey := "policy:" + s.region + ":" + tier
		raw, err := s.rdb.Get(tctx, redisKey).Result()
		if err != nil {
			if err != redis.Nil {
				log.Printf("[policy] GET %s: %v — using static default", redisKey, err)
			}
			s.setStatic(tier)
			continue
		}

		var c4 contract4
		if err := json.Unmarshal([]byte(raw), &c4); err != nil {
			log.Printf("[policy] parse %s: %v — using static default", redisKey, err)
			s.setStatic(tier)
			continue
		}

		if err := s.validate(c4, tier); err != nil {
			log.Printf("[policy] validate %s: %v — using static default", redisKey, err)
			s.setStatic(tier)
			continue
		}

		p := Policy{
			Limit:       c4.LimitPerMinute,
			GlobalLimit: c4.LimitPerMinute,
			Burst:       c4.Burst,
			Algorithm:   c4.Algorithm,
			PolicyID:    c4.PolicyID,
		}
		s.cache[tier] = p
		metrics.PolicyVersion.WithLabelValues(s.region, tier).Set(float64(seqFromID(c4.PolicyID)))
	}
}

// setStatic writes the static default for tier into cache and resets the
// version metric to 0.  Must be called with s.mu held for writing.
func (s *Store) setStatic(tier string) {
	if p, err := Lookup(tier); err == nil {
		s.cache[tier] = p
		metrics.PolicyVersion.WithLabelValues(s.region, tier).Set(0)
	}
}

// validate checks the contract4 fields that the gateway cares about.
func (s *Store) validate(c contract4, tier string) error {
	if c.LimitPerMinute <= 0 {
		return errorf("limit_per_minute must be > 0, got %d", c.LimitPerMinute)
	}
	if c.Burst < 0 {
		return errorf("burst must be >= 0, got %d", c.Burst)
	}
	if !validAlgorithms[c.Algorithm] {
		return errorf("unknown algorithm %q", c.Algorithm)
	}
	if c.Region != s.region {
		return errorf("region mismatch: policy has %q, store is %q", c.Region, s.region)
	}
	if c.Tier != tier {
		return errorf("tier mismatch: policy has %q, key says %q", c.Tier, tier)
	}
	if c.PolicyID == "" {
		return errorf("empty policy_id")
	}
	return nil
}

// seqFromID parses the sequence number out of "pol_<timestamp>_<seq>".
// Returns 1 (non-zero, non-static) if parsing fails.
func seqFromID(id string) int {
	parts := strings.Split(id, "_")
	if len(parts) >= 3 {
		if n, err := strconv.Atoi(parts[len(parts)-1]); err == nil {
			return n
		}
	}
	return 1
}

func errorf(format string, args ...any) error {
	return fmt.Errorf(format, args...)
}
