package handler

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/limiter"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/metrics"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/override"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/policy"
	"github.com/redis/go-redis/v9"
)

const syncChannel = "rl:sync:counter"

// syncMessage is the cross-region pub/sub payload (Contract — Phase 3).
type syncMessage struct {
	Tier     string `json:"tier"`
	UserID   string `json:"user_id"`
	WindowID int64  `json:"window_id"`
	Region   string `json:"region"`
	Value    int64  `json:"value"`
	TsMs     int64  `json:"ts_ms"`
}

// Deps holds the dependencies injected into handlers at startup.
type Deps struct {
	// Limiters maps algorithm name ("token_bucket", "sliding_window") to its
	// implementation. The handler selects the algorithm from the active policy.
	// For unit tests that don't exercise algorithm selection, registering a
	// single fake under both keys is sufficient.
	Limiters map[string]limiter.Limiter

	Region string

	// Rdb is the local Redis client used for Phase 3 global cap, HINCRBY, and
	// pub/sub publish. Nil in unit tests (global check skipped).
	Rdb redis.Cmdable

	// PolicyStore is the Redis-backed policy cache. When nil, the handler falls
	// back to the static policy.Lookup — used in unit tests.
	PolicyStore *policy.Store

	// Overrides is the per-user override cache. When nil, overrides are skipped
	// — used in unit tests.
	Overrides *override.Cache
}

// Register attaches all routes to the given router.
func Register(r *gin.Engine, d Deps) {
	r.GET("/health", health(d.Region))
	r.POST("/check", check(d))
}

func health(region string) gin.HandlerFunc {
	return func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok", "region": region})
	}
}

type checkRequest struct {
	UserID   string `json:"user_id"  binding:"required"`
	Tier     string `json:"tier"     binding:"required,oneof=free premium internal"`
	Region   string `json:"region"   binding:"required,oneof=us eu asia"`
	Endpoint string `json:"endpoint" binding:"required"`
}

type checkResponse struct {
	Allowed      bool   `json:"allowed"`
	Remaining    int    `json:"remaining"`
	Limit        int    `json:"limit"`
	RetryAfterMs int    `json:"retry_after_ms"`
	PolicyID     string `json:"policy_id"`
}

func check(d Deps) gin.HandlerFunc {
	return func(c *gin.Context) {
		var req checkRequest
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}

		// A regional gateway only makes decisions for its own region.
		if req.Region != d.Region {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": fmt.Sprintf("gateway region is %q, request region is %q", d.Region, req.Region),
			})
			return
		}

		// Resolve the active policy — dynamic store if wired, else static fallback.
		var pol policy.Policy
		var err error
		if d.PolicyStore != nil {
			pol, err = d.PolicyStore.Get(req.Tier)
		} else {
			pol, err = policy.Lookup(req.Tier)
		}
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}

		// Apply per-user override (limit_per_minute only; burst/algorithm unchanged).
		effectiveLimit := pol.Limit
		if d.Overrides != nil {
			if overLimit, hasOverride := d.Overrides.Lookup(c.Request.Context(), req.UserID); hasOverride {
				effectiveLimit = overLimit
			}
		}

		// Select limiter by algorithm name from the policy.
		lim, ok := d.Limiters[pol.Algorithm]
		if !ok {
			// Unknown algorithm in policy — degrade to first registered limiter and log.
			log.Printf("[warn] unknown algorithm %q in policy %s — using fallback", pol.Algorithm, pol.PolicyID)
			for _, v := range d.Limiters {
				lim = v
				break
			}
		}

		key := fmt.Sprintf("rl:local:%s:%s:%s", req.Region, req.Tier, req.UserID)

		start := time.Now()
		allowed, remaining, retryMs, err := lim.Check(c.Request.Context(), key, effectiveLimit, pol.Burst)
		elapsed := time.Since(start)

		metrics.DecisionDuration.WithLabelValues(d.Region).Observe(elapsed.Seconds())

		// Phase 3: G-Counter increment + global cap check.
		// Runs for every request that reached the rate-limit decision
		// (region-validated, Rdb wired), regardless of local bucket outcome.
		if err == nil && d.Rdb != nil {
			windowID := time.Now().Unix() / 60
			globalKey := fmt.Sprintf("rl:global:%s:%s:%d", req.Tier, req.UserID, windowID)
			ctx := c.Request.Context()

			newSlot, incrErr := d.Rdb.HIncrBy(ctx, globalKey, d.Region, 1).Result()
			if incrErr != nil {
				log.Printf("[warn] global HIncrBy %s: %v — skipping global check", globalKey, incrErr)
			} else {
				d.Rdb.Expire(ctx, globalKey, 120*time.Second)

				if payload, mErr := json.Marshal(syncMessage{
					Tier:     req.Tier,
					UserID:   req.UserID,
					WindowID: windowID,
					Region:   d.Region,
					Value:    newSlot,
					TsMs:     time.Now().UnixMilli(),
				}); mErr == nil {
					d.Rdb.Publish(ctx, syncChannel, string(payload))
				}

				rawSlots, hErr := d.Rdb.HGetAll(ctx, globalKey).Result()
				if hErr != nil {
					log.Printf("[warn] global HGetAll %s: %v — skipping global check", globalKey, hErr)
				} else {
					sum := 0
					for _, v := range rawSlots {
						if n, err2 := strconv.Atoi(v); err2 == nil {
							sum += n
						}
					}

					if sum > pol.GlobalLimit {
						allowed = false
						remaining = 0
						retryMs = int((windowID+1)*60*1000 - time.Now().UnixMilli())
						if retryMs < 0 {
							retryMs = 0
						}
					}

					if sum*2 >= pol.GlobalLimit {
						metrics.CounterValue.WithLabelValues(
							d.Region, req.Tier, req.UserID,
						).Set(float64(sum))
					}
				}
			}
		}

		decision := "allowed"
		if err != nil {
			decision = "error"
			allowed = false
			remaining = 0
			retryMs = 1000
		} else if !allowed {
			decision = "denied"
		}

		metrics.RequestsTotal.WithLabelValues(d.Region, req.Tier, req.Endpoint, decision).Inc()

		c.JSON(http.StatusOK, checkResponse{
			Allowed:      allowed,
			Remaining:    remaining,
			Limit:        effectiveLimit,
			RetryAfterMs: retryMs,
			PolicyID:     pol.PolicyID,
		})
	}
}
