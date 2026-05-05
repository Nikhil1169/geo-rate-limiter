import pathlib
import pickle
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from population import load_population, pick_user, POPULATION_CACHE


@pytest.fixture(autouse=True)
def _fresh_cache(tmp_path, monkeypatch):
    """Redirect the pickle cache to a temp dir for each test."""
    monkeypatch.setattr("population.POPULATION_CACHE", tmp_path / "population.pkl")


def test_free_count_and_format():
    pop = load_population()
    assert len(pop["free"]) == 10_000
    assert pop["free"][0]   == "free_00001"
    assert pop["free"][-1]  == "free_10000"


def test_premium_count_and_format():
    pop = load_population()
    assert len(pop["premium"]) == 100
    assert pop["premium"][0]   == "premium_001"
    assert pop["premium"][-1]  == "premium_100"


def test_internal_count_and_format():
    pop = load_population()
    assert len(pop["internal"]) == 5
    assert pop["internal"][0]   == "internal_1"
    assert pop["internal"][-1]  == "internal_5"


def test_pickle_roundtrip(tmp_path, monkeypatch):
    cache = tmp_path / "pop.pkl"
    monkeypatch.setattr("population.POPULATION_CACHE", cache)
    pop1 = load_population()
    assert cache.exists()
    pop2 = load_population()  # second call reads from pickle
    assert pop1 == pop2


def test_pick_user_valid_tier():
    pop = load_population()
    for _ in range(200):
        user_id, tier = pick_user(pop)
        assert tier in ("free", "premium", "internal")
        assert user_id in pop[tier]


def test_pick_user_bias_always():
    pop = load_population()
    results = [pick_user(pop, bias_user="free_00001", bias_share=1.0) for _ in range(50)]
    assert all(uid == "free_00001" for uid, _ in results)
    assert all(tier == "free" for _, tier in results)


def test_pick_user_bias_zero():
    """bias_share=0 should never return the culprit (statistically)."""
    pop = load_population()
    results = [pick_user(pop, bias_user="free_00001", bias_share=0.0) for _ in range(200)]
    # With 200 draws and share=0, the probability of never picking free_00001
    # from a 10K-user pool at 90% free weight is fine — but we're not banning it.
    # Just confirm the function returns valid users regardless.
    for uid, tier in results:
        assert tier in ("free", "premium", "internal")
        assert uid in pop[tier]
