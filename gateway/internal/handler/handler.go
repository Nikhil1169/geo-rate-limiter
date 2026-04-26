package handler

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/limiter"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/metrics"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/policy"
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
	Limiter limiter.Limiter
	Region  string
	// Rdb is the local Redis client used for the Phase 3 global cap check,
	// HINCRBY, and pub/sub publish.  Nil in unit tests (global check skipped).
	Rdb redis.Cmdable
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
		// The simulator is expected to route requests by region; a mismatch
		// here indicates a mis-routed request.
		if req.Region != d.Region {
			c.JSON(http.StatusBadRequest, gin.H{
				"error": fmt.Sprintf("gateway region is %q, request region is %q", d.Region, req.Region),
			})
			return
		}

		pol, err := policy.Lookup(req.Tier)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}

		key := fmt.Sprintf("rl:local:%s:%s:%s", req.Region, req.Tier, req.UserID)

		start := time.Now()
		allowed, remaining, retryMs, err := d.Limiter.Check(c.Request.Context(), key, pol.Limit, pol.Burst)
		elapsed := time.Since(start)

		metrics.DecisionDuration.WithLabelValues(d.Region).Observe(elapsed.Seconds())

		// Phase 3: cross-region global cap check.
		// Only runs when the local bucket allowed the request and Rdb is wired.
		// Reads the G-Counter from LOCAL Redis (kept up-to-date by the sync
		// service) — no cross-region call on the hot path.
		if err == nil && allowed && d.Rdb != nil {
			windowID := time.Now().Unix() / 60
			globalKey := fmt.Sprintf("rl:global:%s:%s:%d", req.Tier, req.UserID, windowID)
			ctx := c.Request.Context()

			rawSlots, hErr := d.Rdb.HGetAll(ctx, globalKey).Result()
			if hErr != nil {
				log.Printf("[warn] global HGetAll %s: %v — falling back to local decision", globalKey, hErr)
			} else {
				sum := 0
				for _, v := range rawSlots {
					if n, err2 := strconv.Atoi(v); err2 == nil {
						sum += n
					}
				}

				if sum >= pol.GlobalLimit {
					// Global cap exceeded: override the local-allowed decision.
					allowed = false
					remaining = 0
					retryMs = int((windowID+1)*60*1000 - time.Now().UnixMilli())
					if retryMs < 0 {
						retryMs = 0
					}
				} else {
					// Consume one slot in the G-Counter and notify peer sync services.
					newSlot, incrErr := d.Rdb.HIncrBy(ctx, globalKey, d.Region, 1).Result()
					if incrErr == nil {
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
						// Emit per-user gauge only for users at >50% of the global cap
						// to bound Prometheus cardinality (top-N approximation).
						if newSum := sum + 1; newSum*2 >= pol.GlobalLimit {
							metrics.CounterValue.WithLabelValues(
								d.Region, req.Tier, req.UserID,
							).Set(float64(newSum))
						}
					}
				}
			}
		}

		decision := "allowed"
		if err != nil {
			// Degrade-closed: deny the request but don't crash. The Redis error
			// is operational — the caller should retry after a short back-off.
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
			Limit:        pol.Limit,
			RetryAfterMs: retryMs,
			PolicyID:     pol.PolicyID,
		})
	}
}
