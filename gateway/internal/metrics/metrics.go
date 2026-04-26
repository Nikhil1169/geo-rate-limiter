package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

// RequestsTotal counts every /check decision — Contract 3.
var RequestsTotal = promauto.NewCounterVec(
	prometheus.CounterOpts{
		Name: "rl_requests_total",
		Help: "Total rate-limit decisions by region, tier, endpoint, and outcome.",
	},
	[]string{"region", "tier", "endpoint", "decision"},
)

// DecisionDuration measures the wall-clock time of each /check decision — Contract 3.
// Buckets are tighter than Prometheus defaults because the Lua EVAL runs in ~28µs
// locally; the default [5ms, 10ms, ...] range would bury all signal in one bucket.
var DecisionDuration = promauto.NewHistogramVec(
	prometheus.HistogramOpts{
		Name:    "rl_decision_duration_seconds",
		Help:    "Latency of each rate-limit decision, from request receipt to Redis return.",
		Buckets: []float64{0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25},
	},
	[]string{"region"},
)

// CounterValue exposes the per-user global request count — Contract 3.
// Emitted only for users whose global sum exceeds 50% of their tier cap
// to bound Prometheus cardinality. Phase 4's agent reads this to detect spikes.
var CounterValue = promauto.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "rl_counter_value",
		Help: "Current global request count for a user (sampled: only emitted at >50% of cap).",
	},
	[]string{"region", "tier", "user_id"},
)

// PolicyVersion tracks the active policy generation per region+tier — Contract 3.
// 0 = static baseline; positive integers are agent-written sequence numbers (Phase 4).
var PolicyVersion = promauto.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "rl_policy_version",
		Help: "Active policy version per region and tier (0 = static baseline).",
	},
	[]string{"region", "tier"},
)
