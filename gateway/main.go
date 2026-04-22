package main

import (
	"context"
	"flag"
	"log"
	"os"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/handler"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/limiter"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/metrics"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/override"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/policy"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
)

func flagOrEnv(name, envKey, def string) *string {
	if v := os.Getenv(envKey); v != "" {
		def = v
	}
	return flag.String(name, def, "")
}

func main() {
	region    := flagOrEnv("region",     "REGION",     "us")
	redisAddr := flagOrEnv("redis-addr", "REDIS_ADDR", "localhost:6379")
	listen    := flagOrEnv("listen",     "LISTEN",     ":8080")
	flag.Parse()

	ctx := context.Background()

	rdb := redis.NewClient(&redis.Options{Addr: *redisAddr})
	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("redis ping %s: %v", *redisAddr, err)
	}
	log.Printf("connected to redis at %s", *redisAddr)

	// Build and load both limiters.
	tb := limiter.NewTokenBucket(rdb)
	if err := tb.Load(ctx); err != nil {
		log.Fatalf("load token bucket script: %v", err)
	}
	sw := limiter.NewSlidingWindow(rdb)
	if err := sw.Load(ctx); err != nil {
		log.Fatalf("load sliding window script: %v", err)
	}
	log.Printf("limiter scripts loaded, region=%s", *region)

	// Initialise policy-version gauge to 0 (static baseline) for each tier.
	for _, tier := range []string{"free", "premium", "internal"} {
		metrics.PolicyVersion.WithLabelValues(*region, tier).Set(0)
	}

	// Start the dynamic policy store (polls Redis every 5 s).
	pStore := policy.NewStore(rdb, *region)
	pStore.Start(ctx)
	log.Printf("policy store started, region=%s", *region)

	// Start the per-user override cache (5 s TTL, no background goroutine needed).
	ovCache := override.New(rdb, 5*time.Second)

	r := gin.New()
	r.Use(gin.Recovery())
	handler.Register(r, handler.Deps{
		Limiters: map[string]limiter.Limiter{
			"token_bucket":   tb,
			"sliding_window": sw,
		},
		Region:      *region,
		Rdb:         rdb,
		PolicyStore: pStore,
		Overrides:   ovCache,
	})
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	log.Printf("listening on %s", *listen)
	if err := r.Run(*listen); err != nil {
		log.Fatalf("run: %v", err)
	}
}
