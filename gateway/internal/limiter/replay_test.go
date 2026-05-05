//go:build replay

package limiter_test

// T3: replay determinism.
//
// Run with: go test -tags=replay -v -run TestTokenBucket_Replay ./internal/limiter/
//
// Phase 1: executes a deterministic sequence of evalBucket calls and captures
// every (inputs, outputs) tuple to a temp JSONL file.
// Phase 2: flushes the key and replays the same inputs; asserts outputs match.
// Any divergence means the script depends on hidden state (time, random, external).

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"
)

type bucketCall struct {
	Key          string `json:"key"`
	Limit        int64  `json:"limit"`
	Burst        int64  `json:"burst"`
	NowMs        int64  `json:"now_ms"`
	Allowed      int64  `json:"allowed"`
	Remaining    int64  `json:"remaining"`
	RetryAfterMs int64  `json:"retry_after_ms"`
}

func TestTokenBucket_Replay(t *testing.T) {
	ctx := context.Background()
	rdb := redisClient(t)
	script := loadScript(t)

	const (
		limit = 120 // 2 tokens/sec — gives a mix of allows and denials in the sequence
		burst = 5
		calls = 20
		stepMs = 250 // synthetic 250 ms between each call
	)

	key := "test:tb:replay"
	rdb.Del(ctx, key)

	baseMs := time.Now().UnixMilli()

	// ── Phase 1: capture ────────────────────────────────────────────────────
	capture := make([]bucketCall, 0, calls)
	for i := 0; i < calls; i++ {
		nowMs := baseMs + int64(i)*stepMs
		allowed, remaining, retry, err := evalBucket(ctx, script, rdb, key, limit, burst, nowMs)
		if err != nil {
			t.Fatalf("capture call %d: %v", i, err)
		}
		capture = append(capture, bucketCall{
			Key: key, Limit: limit, Burst: burst, NowMs: nowMs,
			Allowed: allowed, Remaining: remaining, RetryAfterMs: retry,
		})
	}

	f, err := os.CreateTemp("", "tb_replay_*.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	defer os.Remove(f.Name())

	enc := json.NewEncoder(f)
	for _, c := range capture {
		if err := enc.Encode(c); err != nil {
			t.Fatal(err)
		}
	}
	f.Close()
	t.Logf("capture: %d calls written to %s", calls, f.Name())

	// ── Phase 2: replay ──────────────────────────────────────────────────────
	rdb.Del(ctx, key)

	captureFile, err := os.Open(f.Name())
	if err != nil {
		t.Fatal(err)
	}
	defer captureFile.Close()

	dec := json.NewDecoder(captureFile)
	i := 0
	for dec.More() {
		var c bucketCall
		if err := dec.Decode(&c); err != nil {
			t.Fatalf("decode call %d: %v", i, err)
		}
		allowed, remaining, retry, err := evalBucket(ctx, script, rdb, c.Key, c.Limit, c.Burst, c.NowMs)
		if err != nil {
			t.Fatalf("replay call %d: %v", i, err)
		}
		if allowed != c.Allowed || remaining != c.Remaining || retry != c.RetryAfterMs {
			t.Errorf("call %d mismatch:\n  got  {allowed=%d remaining=%d retry=%d}\n  want {allowed=%d remaining=%d retry=%d}",
				i, allowed, remaining, retry, c.Allowed, c.Remaining, c.RetryAfterMs)
		}
		i++
	}
	t.Logf("replay: %d calls verified", i)
}
