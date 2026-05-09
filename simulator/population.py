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
    bias_users: list[tuple[str, float]] | None = None,
) -> tuple[str, str]:
    """Return (user_id, tier).

    bias_users takes priority: list of (user_id, share) pairs applied cumulatively;
    shares must sum to < 1.0 (remainder goes to random population).
    Falls back to legacy bias_user / bias_share when bias_users is not set.
    """
    if bias_users:
        r = random.random()
        cumulative = 0.0
        for uid, share in bias_users:
            cumulative += share
            if r < cumulative:
                tier = uid.split("_")[0]
                return uid, tier
    elif bias_user and random.random() < bias_share:
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
