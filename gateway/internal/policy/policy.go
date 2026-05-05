package policy

import "fmt"

// Policy holds the rate-limit parameters for a tier.
// PolicyID uses pol_static_* to distinguish hardcoded baselines from
// agent-written policies (pol_<timestamp>_<seq>) that come in Phase 4.
type Policy struct {
	Limit       int
	GlobalLimit int    // Contract 4: global_limit_per_minute — cap across all regions combined
	Burst       int
	Algorithm   string // "token_bucket" or "sliding_window" — selects the Limiter in handler
	PolicyID    string
}

var static = map[string]Policy{
	"free":     {Limit: 10, GlobalLimit: 10, Burst: 5, Algorithm: "token_bucket", PolicyID: "pol_static_free"},
	"premium":  {Limit: 100, GlobalLimit: 100, Burst: 20, Algorithm: "token_bucket", PolicyID: "pol_static_premium"},
	"internal": {Limit: 1000, GlobalLimit: 1000, Burst: 100, Algorithm: "token_bucket", PolicyID: "pol_static_internal"},
}

// Lookup returns the policy for the given tier, or an error if the tier is unknown.
func Lookup(tier string) (Policy, error) {
	p, ok := static[tier]
	if !ok {
		return Policy{}, fmt.Errorf("unknown tier %q", tier)
	}
	return p, nil
}
