package main

import (
	"context"
	"flag"
	"log"
	"os"

	"github.com/gin-gonic/gin"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/handler"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/limiter"
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

	lim := limiter.NewTokenBucket(rdb)
	if err := lim.Load(ctx); err != nil {
		log.Fatalf("load token bucket script: %v", err)
	}
	log.Printf("token bucket script loaded, region=%s", *region)

	r := gin.New()
	r.Use(gin.Recovery())
	handler.Register(r, handler.Deps{Limiter: lim, Region: *region})
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	log.Printf("listening on %s", *listen)
	if err := r.Run(*listen); err != nil {
		log.Fatalf("run: %v", err)
	}
}
