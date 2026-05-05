package policy_test

import (
	"testing"

	"github.com/nikhil/geo-rate-limiter/gateway/internal/policy"
)

func TestLookup(t *testing.T) {
	cases := []struct {
		tier      string
		wantLimit int
		wantBurst int
		wantOK    bool
	}{
		{"free", 10, 5, true},
		{"premium", 100, 20, true},
		{"internal", 1000, 100, true},
		{"unknown", 0, 0, false},
		{"", 0, 0, false},
	}
	for _, tc := range cases {
		p, err := policy.Lookup(tc.tier)
		if tc.wantOK {
			if err != nil {
				t.Errorf("tier=%q: unexpected error: %v", tc.tier, err)
				continue
			}
			if p.Limit != tc.wantLimit || p.Burst != tc.wantBurst {
				t.Errorf("tier=%q: got {%d,%d} want {%d,%d}", tc.tier, p.Limit, p.Burst, tc.wantLimit, tc.wantBurst)
			}
			if p.PolicyID == "" {
				t.Errorf("tier=%q: empty PolicyID", tc.tier)
			}
		} else {
			if err == nil {
				t.Errorf("tier=%q: expected error, got nil", tc.tier)
			}
		}
	}
}
