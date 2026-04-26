package handler

import (
	"fmt"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/limiter"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/metrics"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/policy"
)

// Deps holds the dependencies injected into handlers at startup.
type Deps struct {
	Limiter limiter.Limiter
	Region  string
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
