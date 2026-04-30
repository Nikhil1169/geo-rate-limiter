import pathlib
import pickle
import random

POPULATION_CACHE = pathlib.Path(__file__).parent / "population.pkl"

# Tier sampling weights (must sum to 1.0)
TIER_WEIGHTS = {"free": 0.90, "premium": 0.09, "internal": 0.01}

_ENDPOINTS = [
    "/api/v1/data",
    "/api/v1/search",
    "/api/v1/write",
    "/api/v1/status",
]


def _generate() -> dict[str, list[str]]:
    return {
        "free":     [f"free_{i:05d}" for i in range(1, 10_001)],
        "premium":  [f"premium_{i:03d}" for i in range(1, 101)],
        "internal": [f"internal_{i}" for i in range(1, 6)],
    }


def load_population() -> dict[str, list[str]]:
    if POPULATION_CACHE.exists():
        with POPULATION_CACHE.open("rb") as f:
            return pickle.load(f)
    pop = _generate()
    with POPULATION_CACHE.open("wb") as f:
        pickle.dump(pop, f)
    return pop


def pick_user(
    population: dict[str, list[str]],
    *,
    bias_user: str | None = None,
    bias_share: float = 0.0,
) -> tuple[str, str]:
    """Return (user_id, tier). If bias_user is set, return it with probability bias_share."""
    if bias_user and random.random() < bias_share:
        tier = bias_user.split("_")[0]
        return bias_user, tier
    tier = random.choices(
        list(TIER_WEIGHTS.keys()),
        weights=list(TIER_WEIGHTS.values()),
        k=1,
    )[0]
    return random.choice(population[tier]), tier


def pick_endpoint() -> str:
    return random.choice(_ENDPOINTS)
