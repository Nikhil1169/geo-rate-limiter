import asyncio
import dataclasses
from typing import TYPE_CHECKING

from engine import run, poisson_stream
from population import load_population

if TYPE_CHECKING:
    from stats import LiveStats


@dataclasses.dataclass
class StreamConfig:
    rps:        float
    duration:   float
    region:     str
    diurnal:    bool        = False
    bias_user:  str | None  = None
    bias_share: float       = 0.0
    # Multi-user bias: list of (user_id, share) pairs; shares must sum < 1.0
    bias_users: list[tuple[str, float]] | None = None


@dataclasses.dataclass
class ScenarioSpec:
    name:        str
    description: str
    # Streams sharing a region are run sequentially (chained).
    # Streams with distinct regions run concurrently via asyncio.gather.
    streams:     list[StreamConfig]


SCENARIOS: dict[str, ScenarioSpec] = {
    "product_launch": ScenarioSpec(
        name="product_launch",
        description="10x spike on US after 30s baseline; EU and Asia stay at baseline for full run",
        streams=[
            # US: baseline 30s then spike 120s — same region, so chained sequentially
            StreamConfig(rps=5.0,  duration=30.0,  region="us"),
            StreamConfig(rps=50.0, duration=120.0, region="us"),
            # EU and Asia run concurrently for the full 150s
            StreamConfig(rps=5.0, duration=150.0, region="eu"),
            StreamConfig(rps=3.0, duration=150.0, region="asia"),
        ],
    ),
    "noisy_neighbor": ScenarioSpec(
        name="noisy_neighbor",
        description=(
            "Two abusers (free_00001 at 35%, free_00002 at 25%) saturate the US free tier "
            "for 120s. Combined 60% share drives tier congestion above the threshold, "
            "injecting ~200ms backend delay for ALL users including legit ones (free_00003–00008). "
            "Agent detects both abusers at ~t=60s and creates overrides (30/min each). "
            "Congestion drops, legit user latency recovers from ~200ms to <10ms."
        ),
        streams=[
            StreamConfig(
                rps=20.0, duration=120.0, region="us",
                bias_users=[
                    ("free_00001", 0.35),
                    ("free_00002", 0.25),
                ],
            ),
        ],
    ),
    "global_steady": ScenarioSpec(
        name="global_steady",
        description="Balanced multi-region traffic with diurnal sine overlay (5-minute window)",
        streams=[
            StreamConfig(rps=10.0, duration=300.0, region="us",   diurnal=True),
            StreamConfig(rps=6.0,  duration=300.0, region="eu",   diurnal=True),
            StreamConfig(rps=4.0,  duration=300.0, region="asia", diurnal=True),
        ],
    ),
    "region_failover": ScenarioSpec(
        name="region_failover",
        description=(
            "All three regions for 120s. Kill gateway-us manually at ~t=60 "
            "(`docker stop geo-rate-limiter-gateway-us-1`) to observe failover. "
            "US requests will appear as errors in stats."
        ),
        streams=[
            StreamConfig(rps=15.0, duration=120.0, region="us"),
            StreamConfig(rps=8.0,  duration=120.0, region="eu"),
            StreamConfig(rps=6.0,  duration=120.0, region="asia"),
        ],
    ),
}


async def _run_stream_group(
    configs: list[StreamConfig],
    population: dict[str, list[str]],
    stats: "LiveStats",
    *,
    concurrency: int,
    client,
    semaphore,
) -> None:
    """Run a list of same-region StreamConfigs sequentially (chained)."""
    for cfg in configs:
        stream = poisson_stream(
            cfg.rps, cfg.duration, population, cfg.region,
            diurnal=cfg.diurnal,
            bias_user=cfg.bias_user,
            bias_share=cfg.bias_share,
            bias_users=cfg.bias_users,
        )
        await run(stream, stats, concurrency=concurrency, client=client, semaphore=semaphore)


async def run_scenario(
    name: str,
    stats: "LiveStats",
    *,
    concurrency: int = 50,
) -> None:
    if name not in SCENARIOS:
        available = ", ".join(sorted(SCENARIOS))
        raise KeyError(f"Unknown scenario '{name}'. Available: {available}")

    spec = SCENARIOS[name]
    population = load_population()

    # Group configs by region to determine sequential vs concurrent execution
    by_region: dict[str, list[StreamConfig]] = {}
    for cfg in spec.streams:
        by_region.setdefault(cfg.region, []).append(cfg)

    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        import asyncio as _asyncio
        semaphore = _asyncio.Semaphore(concurrency)
        # Each region group runs concurrently; within a group, streams are chained
        await asyncio.gather(*[
            _run_stream_group(configs, population, stats,
                              concurrency=concurrency, client=client, semaphore=semaphore)
            for configs in by_region.values()
        ])


def list_scenarios() -> list[str]:
    return sorted(SCENARIOS)
