package handler_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/handler"
	"github.com/nikhil/geo-rate-limiter/gateway/internal/limiter"
)

func init() {
	gin.SetMode(gin.TestMode)
}

// fakeLimiter is a test double for the Limiter interface.
type fakeLimiter struct {
	allowed      bool
	remaining    int
	retryAfterMs int
	err          error
	lastKey      string
}

func (f *fakeLimiter) Check(_ context.Context, key string, _, _ int) (bool, int, int, error) {
	f.lastKey = key
	return f.allowed, f.remaining, f.retryAfterMs, f.err
}

// fakeDepsWith registers fl under all algorithm keys so tests that don't
// exercise algorithm selection work without change. PolicyStore and Overrides
// are left nil — the handler falls back to static policy.Lookup and skips
// overrides when these are nil.
func fakeDepsWith(fl *fakeLimiter, region string) handler.Deps {
	return handler.Deps{
		Limiters: map[string]limiter.Limiter{
			"token_bucket":   fl,
			"sliding_window": fl,
		},
		Region: region,
	}
}

func newRouter(d handler.Deps) *gin.Engine {
	r := gin.New()
	handler.Register(r, d)
	return r
}

func post(r *gin.Engine, body any) *httptest.ResponseRecorder {
	b, _ := json.Marshal(body)
	req := httptest.NewRequest(http.MethodPost, "/check", bytes.NewReader(b))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func TestCheck_AllowedResponse(t *testing.T) {
	fl := &fakeLimiter{allowed: true, remaining: 4}
	r := newRouter(fakeDepsWith(fl, "us"))

	w := post(r, map[string]any{
		"user_id":  "u1",
		"tier":     "free",
		"region":   "us",
		"endpoint": "/api/x",
	})

	if w.Code != http.StatusOK {
		t.Fatalf("status=%d want 200", w.Code)
	}
	var resp map[string]any
	json.Unmarshal(w.Body.Bytes(), &resp)

	if resp["allowed"] != true {
		t.Errorf("allowed=%v want true", resp["allowed"])
	}
	if resp["policy_id"] != "pol_static_free" {
		t.Errorf("policy_id=%v want pol_static_free", resp["policy_id"])
	}
	if resp["limit"] != float64(10) {
		t.Errorf("limit=%v want 10", resp["limit"])
	}
	// Verify key construction matches Contract 2 schema
	if fl.lastKey != "rl:local:us:free:u1" {
		t.Errorf("key=%q want rl:local:us:free:u1", fl.lastKey)
	}
}

func TestCheck_DeniedResponse(t *testing.T) {
	fl := &fakeLimiter{allowed: false, remaining: 0, retryAfterMs: 5000}
	r := newRouter(fakeDepsWith(fl, "us"))

	w := post(r, map[string]any{
		"user_id":  "u1",
		"tier":     "free",
		"region":   "us",
		"endpoint": "/api/x",
	})

	if w.Code != http.StatusOK {
		t.Fatalf("status=%d want 200 (always-200 contract)", w.Code)
	}
	var resp map[string]any
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["allowed"] != false {
		t.Errorf("allowed=%v want false", resp["allowed"])
	}
	if resp["retry_after_ms"] != float64(5000) {
		t.Errorf("retry_after_ms=%v want 5000", resp["retry_after_ms"])
	}
}

func TestCheck_RedisError_DegradeClosed(t *testing.T) {
	fl := &fakeLimiter{err: context.DeadlineExceeded}
	r := newRouter(fakeDepsWith(fl, "us"))

	w := post(r, map[string]any{
		"user_id":  "u1",
		"tier":     "free",
		"region":   "us",
		"endpoint": "/api/x",
	})

	// Degrade-closed: still HTTP 200 but allowed=false with retry hint.
	if w.Code != http.StatusOK {
		t.Fatalf("status=%d want 200", w.Code)
	}
	var resp map[string]any
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["allowed"] != false {
		t.Errorf("allowed=%v want false on error", resp["allowed"])
	}
	if resp["retry_after_ms"] != float64(1000) {
		t.Errorf("retry_after_ms=%v want 1000", resp["retry_after_ms"])
	}
}

func TestCheck_WrongRegion_400(t *testing.T) {
	fl := &fakeLimiter{allowed: true}
	r := newRouter(fakeDepsWith(fl, "us"))

	w := post(r, map[string]any{
		"user_id":  "u1",
		"tier":     "free",
		"region":   "eu", // mismatch
		"endpoint": "/api/x",
	})

	if w.Code != http.StatusBadRequest {
		t.Fatalf("status=%d want 400 for region mismatch", w.Code)
	}
}

func TestCheck_BadJSON_400(t *testing.T) {
	fl := &fakeLimiter{allowed: true}
	r := newRouter(fakeDepsWith(fl, "us"))

	req := httptest.NewRequest(http.MethodPost, "/check", bytes.NewBufferString("{invalid"))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("status=%d want 400 for bad JSON", w.Code)
	}
}

func TestCheck_UnknownTier_400(t *testing.T) {
	fl := &fakeLimiter{allowed: true}
	r := newRouter(fakeDepsWith(fl, "us"))

	w := post(r, map[string]any{
		"user_id":  "u1",
		"tier":     "enterprise", // not in oneof
		"region":   "us",
		"endpoint": "/api/x",
	})

	if w.Code != http.StatusBadRequest {
		t.Fatalf("status=%d want 400 for unknown tier", w.Code)
	}
}

func TestHealth(t *testing.T) {
	r := newRouter(fakeDepsWith(&fakeLimiter{}, "eu"))
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status=%d want 200", w.Code)
	}
	var resp map[string]any
	json.Unmarshal(w.Body.Bytes(), &resp)
	if resp["region"] != "eu" {
		t.Errorf("region=%v want eu", resp["region"])
	}
}
