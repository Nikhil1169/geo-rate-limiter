package policy

import "fmt"

// Policy holds the rate-limit parameters for a tier.
// PolicyID uses pol_static_* to distinguish hardcoded baselines from
// agent-written policies (pol_<timestamp>_<seq>) that come in Phase 4.
type Policy struct {
	Limit    int
	Burst    int
	PolicyID string
}

var static = map[string]Policy{
	"free":     {Limit: 10, Burst: 5, PolicyID: "pol_static_free"},
	"premium":  {Limit: 100, Burst: 20, PolicyID: "pol_static_premium"},
	"internal": {Limit: 1000, Burst: 100, PolicyID: "pol_static_internal"},
}

// Lookup returns the policy for the given tier, or an error if the tier is unknown.
func Lookup(tier string) (Policy, error) {
	p, ok := static[tier]
	if !ok {
		return Policy{}, fmt.Errorf("unknown tier %q", tier)
	}
	return p, nil
}
