"""Unit tests for RegionalCounter — CRDT property proofs.

Uses fakeredis (in-process, no daemon required) with Lua support so the
tests run without a live Redis and without Docker.

Properties verified:
  - Monotonicity    max(a, b) >= a for any b
  - Idempotence     merge(x) twice == merge(x) once
  - Commutativity   merge(a then b) == merge(b then a)
  - Associativity   merge(merge(a,b),c) == merge(a,merge(b,c))
  - Independence    one region's slot never affects another's
"""

import pytest
import fakeredis

from sync.counter import RegionalCounter

WIN = 99999999  # fixed window_id for all tests
TIER = "free"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

async def _counter(region: str) -> RegionalCounter:
    """Fresh in-process FakeRedis instance wired to one region."""
    r = fakeredis.FakeAsyncRedis()
    return await RegionalCounter.create(r, region)


async def _shared_counter(r, region: str) -> RegionalCounter:
    """Multiple counters sharing one FakeRedis (multi-region on same node)."""
    return await RegionalCounter.create(r, region)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_counter_all_zeros():
    c = await _counter("us")
    g = await c.get_global(TIER, "u1", WIN)
    assert g == {"us": 0, "eu": 0, "asia": 0}


@pytest.mark.asyncio
async def test_increment_local():
    c = await _counter("us")
    v1 = await c.increment_local(TIER, "u1", WIN)
    v2 = await c.increment_local(TIER, "u1", WIN)
    assert v1 == 1
    assert v2 == 2
    assert await c.get_local(TIER, "u1", WIN) == 2


# ---------------------------------------------------------------------------
# Monotonicity: once a slot reaches value N, merge(< N) is a no-op
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_monotone_no_regression():
    c = await _counter("eu")
    await c.merge_remote(TIER, "u1", WIN, "us", 5)
    # Applying a smaller value must not decrease the slot.
    await c.merge_remote(TIER, "u1", WIN, "us", 3)
    g = await c.get_global(TIER, "u1", WIN)
    assert g["us"] == 5


@pytest.mark.asyncio
async def test_monotone_advances_on_larger():
    c = await _counter("eu")
    await c.merge_remote(TIER, "u1", WIN, "us", 3)
    await c.merge_remote(TIER, "u1", WIN, "us", 7)
    g = await c.get_global(TIER, "u1", WIN)
    assert g["us"] == 7


# ---------------------------------------------------------------------------
# Idempotence: applying the same message N times == applying it once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotent_same_value():
    c = await _counter("asia")
    for _ in range(5):
        await c.merge_remote(TIER, "u2", WIN, "eu", 4)
    g = await c.get_global(TIER, "u2", WIN)
    assert g["eu"] == 4


# ---------------------------------------------------------------------------
# Commutativity: order of message arrival does not matter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commutative_two_region_updates():
    # Scenario: messages {us=4, eu=2} arrive in different orders but
    # must converge to the same state.

    shared = fakeredis.FakeAsyncRedis()

    # Path A: apply us=4, then eu=2
    c_a = await _shared_counter(shared, "asia")
    await c_a.merge_remote(TIER, "u3", WIN, "us", 4)
    await c_a.merge_remote(TIER, "u3", WIN, "eu", 2)
    result_a = await c_a.get_global(TIER, "u3", WIN)

    # Path B: fresh state, apply eu=2, then us=4
    shared_b = fakeredis.FakeAsyncRedis()
    c_b = await _shared_counter(shared_b, "asia")
    await c_b.merge_remote(TIER, "u3", WIN, "eu", 2)
    await c_b.merge_remote(TIER, "u3", WIN, "us", 4)
    result_b = await c_b.get_global(TIER, "u3", WIN)

    assert result_a == result_b
    assert result_a == {"us": 4, "eu": 2, "asia": 0}


# ---------------------------------------------------------------------------
# Associativity: grouping of merges does not matter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_associative_three_values():
    # (merge(a,b)) merged with c  ==  a merged with (merge(b,c))
    #
    # Concrete: three successive observations of the us slot:
    #   obs_1=3, obs_2=6, obs_3=5  (out-of-order delivery)
    # All orderings must settle at 6.

    observations = [("us", 3), ("us", 6), ("us", 5)]

    for perm in [
        [0, 1, 2],
        [0, 2, 1],
        [1, 0, 2],
        [1, 2, 0],
        [2, 0, 1],
        [2, 1, 0],
    ]:
        r = fakeredis.FakeAsyncRedis()
        c = await _shared_counter(r, "eu")
        for idx in perm:
            region, value = observations[idx]
            await c.merge_remote(TIER, "u4", WIN, region, value)
        g = await c.get_global(TIER, "u4", WIN)
        assert g["us"] == 6, f"failed for permutation {perm}: got {g}"


# ---------------------------------------------------------------------------
# Independence: slots don't bleed into each other
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slots_independent():
    c = await _counter("us")
    await c.merge_remote(TIER, "u5", WIN, "eu", 10)
    await c.merge_remote(TIER, "u5", WIN, "asia", 7)
    g = await c.get_global(TIER, "u5", WIN)
    assert g["eu"] == 10
    assert g["asia"] == 7
    assert g["us"] == 0   # us untouched


# ---------------------------------------------------------------------------
# global_sum helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_sum():
    c = await _counter("us")
    await c.increment_local(TIER, "u6", WIN)
    await c.increment_local(TIER, "u6", WIN)
    await c.merge_remote(TIER, "u6", WIN, "eu", 3)
    await c.merge_remote(TIER, "u6", WIN, "asia", 1)
    total = await c.global_sum(TIER, "u6", WIN)
    assert total == 6  # us=2 + eu=3 + asia=1


# ---------------------------------------------------------------------------
# Separate window IDs are isolated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_window_isolation():
    c = await _counter("us")
    await c.merge_remote(TIER, "u7", WIN,     "eu", 5)
    await c.merge_remote(TIER, "u7", WIN + 1, "eu", 2)
    g0 = await c.get_global(TIER, "u7", WIN)
    g1 = await c.get_global(TIER, "u7", WIN + 1)
    assert g0["eu"] == 5
    assert g1["eu"] == 2
