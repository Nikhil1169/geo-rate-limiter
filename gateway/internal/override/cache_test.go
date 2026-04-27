package override_test

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nikhil/geo-rate-limiter/gateway/internal/override"
	"github.com/redis/go-redis/v9"
)

// fakeRedis implements the redisGetter interface used by Cache.
type fakeRedis struct {
	mu      sync.Mutex
	data    map[string]string
	getCalls atomic.Int64
}

func (f *fakeRedis) Get(_ context.Context, key string) *redis.StringCmd {
	f.getCalls.Add(1)
	f.mu.Lock()
	v, ok := f.data[key]
	f.mu.Unlock()

	cmd := redis.NewStringCmd(context.Background(), "get", key)
	if ok {
		cmd.SetVal(v)
	} else {
		cmd.SetErr(redis.Nil)
	}
	return cmd
}

// TestCache_Hit_ReturnsOverride — a positive override is returned correctly.
func TestCache_Hit_ReturnsOverride(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{
		"override:u1": `{"limit_per_minute":5,"ttl":300,"reason":"test"}`,
	}}
	c := override.New(rdb, 5*time.Second)

	limit, has := c.Lookup(context.Background(), "u1")
	if !has {
		t.Fatal("expected hasOverride=true")
	}
	if limit != 5 {
		t.Errorf("limit=%d want 5", limit)
	}
}

// TestCache_NegativeCache — missing key is cached; subsequent calls do not
// hit Redis again within the TTL.
func TestCache_NegativeCache(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{}}
	c := override.New(rdb, 5*time.Second)

	_, has1 := c.Lookup(context.Background(), "u_absent")
	if has1 {
		t.Fatal("expected hasOverride=false for absent key")
	}

	callsBefore := rdb.getCalls.Load()
	_, has2 := c.Lookup(context.Background(), "u_absent")
	if has2 {
		t.Fatal("expected hasOverride=false on second call")
	}
	callsAfter := rdb.getCalls.Load()

	if callsAfter != callsBefore {
		t.Errorf("Redis GET called again within TTL (%d→%d): negative cache not working",
			callsBefore, callsAfter)
	}
}

// TestCache_Expiry — after TTL expires, cache re-fetches from Redis.
func TestCache_Expiry(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{}}
	c := override.New(rdb, 50*time.Millisecond) // very short TTL

	_, _ = c.Lookup(context.Background(), "u_ttl")

	time.Sleep(70 * time.Millisecond) // let entry expire

	rdb.mu.Lock()
	rdb.data["override:u_ttl"] = `{"limit_per_minute":7,"ttl":60,"reason":"after expiry"}`
	rdb.mu.Unlock()

	limit, has := c.Lookup(context.Background(), "u_ttl")
	if !has {
		t.Fatal("expected override after TTL expiry + key appears")
	}
	if limit != 7 {
		t.Errorf("limit=%d want 7", limit)
	}
}

// TestCache_InvalidJSON_NoOverride — unparseable JSON is treated as no override.
func TestCache_InvalidJSON_NoOverride(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{
		"override:u_bad": "{bad json",
	}}
	c := override.New(rdb, 5*time.Second)

	_, has := c.Lookup(context.Background(), "u_bad")
	if has {
		t.Error("expected hasOverride=false for invalid JSON")
	}
}

// TestCache_ZeroLimit_NoOverride — a zero/negative limit_per_minute is rejected.
func TestCache_ZeroLimit_NoOverride(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{
		"override:u_zero": `{"limit_per_minute":0,"ttl":60,"reason":"zero"}`,
	}}
	c := override.New(rdb, 5*time.Second)

	_, has := c.Lookup(context.Background(), "u_zero")
	if has {
		t.Error("expected hasOverride=false for zero limit")
	}
}

// TestCache_Concurrent_SingleFlight — N concurrent callers for the same user
// who all miss the cache together produce exactly one Redis GET.
func TestCache_Concurrent_SingleFlight(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{
		"override:u_concurrent": `{"limit_per_minute":10,"ttl":60,"reason":"parallel"}`,
	}}
	c := override.New(rdb, 5*time.Second)

	const goroutines = 20
	var wg sync.WaitGroup
	start := make(chan struct{})

	wg.Add(goroutines)
	for i := 0; i < goroutines; i++ {
		go func() {
			defer wg.Done()
			<-start
			c.Lookup(context.Background(), "u_concurrent")
		}()
	}

	close(start) // release all goroutines simultaneously
	wg.Wait()

	calls := rdb.getCalls.Load()
	if calls != 1 {
		t.Errorf("Redis GET called %d times for concurrent miss, want 1 (singleflight)", calls)
	}
}
