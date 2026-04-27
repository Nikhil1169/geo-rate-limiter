package policy_test

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/nikhil/geo-rate-limiter/gateway/internal/policy"
	"github.com/redis/go-redis/v9"
)

// fakeRedis implements the redisGetter interface used by policy.Store.
// Only Get is required; other methods are not needed.
type fakeRedis struct {
	data map[string]string
}

func (f *fakeRedis) Get(_ context.Context, key string) *redis.StringCmd {
	cmd := redis.NewStringCmd(context.Background(), "get", key)
	if v, ok := f.data[key]; ok {
		cmd.SetVal(v)
	} else {
		cmd.SetErr(redis.Nil)
	}
	return cmd
}

func validPolicy(region, tier string, limit int, algo string) string {
	p := map[string]interface{}{
		"policy_id":        "pol_1700000000_1",
		"region":           region,
		"tier":             tier,
		"limit_per_minute": limit,
		"burst":            10,
		"algorithm":        algo,
		"ttl_seconds":      300,
		"reason":           "test",
		"created_at":       "2024-01-01T00:00:00Z",
	}
	b, _ := json.Marshal(p)
	return string(b)
}

// TestStore_StaticFallback — empty Redis → Get returns static defaults.
func TestStore_StaticFallback(t *testing.T) {
	s := policy.NewStore(&fakeRedis{data: map[string]string{}}, "us")

	p, err := s.Get("free")
	if err != nil {
		t.Fatalf("Get free: %v", err)
	}
	if p.Limit != 10 {
		t.Errorf("limit=%d want 10", p.Limit)
	}
	if p.Algorithm != "token_bucket" {
		t.Errorf("algorithm=%q want token_bucket", p.Algorithm)
	}
	if p.PolicyID != "pol_static_free" {
		t.Errorf("policy_id=%q want pol_static_free", p.PolicyID)
	}
}

// TestStore_LoadsFromRedis — valid Redis policy → Get returns it.
func TestStore_LoadsFromRedis(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{
		"policy:us:free": validPolicy("us", "free", 3, "sliding_window"),
	}}
	s := policy.NewStore(rdb, "us")
	s.RefreshOnce(context.Background())

	p, err := s.Get("free")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if p.Limit != 3 {
		t.Errorf("limit=%d want 3", p.Limit)
	}
	if p.Algorithm != "sliding_window" {
		t.Errorf("algorithm=%q want sliding_window", p.Algorithm)
	}
	if p.PolicyID != "pol_1700000000_1" {
		t.Errorf("policy_id=%q unexpected", p.PolicyID)
	}
}

// TestStore_InvalidJSON_FallsBackToStatic — bad JSON → static default.
func TestStore_InvalidJSON_FallsBackToStatic(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{
		"policy:us:free": "{not json",
	}}
	s := policy.NewStore(rdb, "us")
	s.RefreshOnce(context.Background())

	p, err := s.Get("free")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if p.PolicyID != "pol_static_free" {
		t.Errorf("policy_id=%q want pol_static_free after bad JSON", p.PolicyID)
	}
}

// TestStore_WrongRegion_FallsBackToStatic — region mismatch → static default.
func TestStore_WrongRegion_FallsBackToStatic(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{
		"policy:us:free": validPolicy("eu", "free", 50, "token_bucket"), // wrong region
	}}
	s := policy.NewStore(rdb, "us")
	s.RefreshOnce(context.Background())

	p, _ := s.Get("free")
	if p.PolicyID != "pol_static_free" {
		t.Errorf("policy_id=%q want pol_static_free after region mismatch", p.PolicyID)
	}
}

// TestStore_UnknownAlgorithm_FallsBackToStatic — unsupported algorithm → static default.
func TestStore_UnknownAlgorithm_FallsBackToStatic(t *testing.T) {
	rdb := &fakeRedis{data: map[string]string{
		"policy:us:free": validPolicy("us", "free", 50, "leaky_bucket"),
	}}
	s := policy.NewStore(rdb, "us")
	s.RefreshOnce(context.Background())

	p, _ := s.Get("free")
	if p.PolicyID != "pol_static_free" {
		t.Errorf("policy_id=%q want pol_static_free after unknown algorithm", p.PolicyID)
	}
}

// TestStore_UnknownTier_Error — Get on an unknown tier returns an error.
func TestStore_UnknownTier_Error(t *testing.T) {
	s := policy.NewStore(&fakeRedis{data: map[string]string{}}, "us")
	_, err := s.Get("enterprise")
	if err == nil {
		t.Error("expected error for unknown tier, got nil")
	}
}
