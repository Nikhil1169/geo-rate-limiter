"""Integration tests for cross-region G-Counter sync.

All tests use in-process FakeAsyncRedis instances (no live Redis, no Docker).
Each FakeAsyncRedis() is an isolated server — mirrors the real three-Redis topology.

Test coverage
  1. test_normal_sync              — messages on redis-us reach eu/asia subscribers.
  2. test_normal_sync_idempotent   — replaying the same absolute value does not double-count.
  3. test_partition_divergence     — eu stays stale when (us→eu) is in PARTITIONS.
  4. test_partition_is_one_way     — (us→eu) does not block the eu→us direction.
  5. test_heal_convergence         — after heal + reconciliation eu converges ≤ 5 s (SLA).
  6. test_heal_bidirectional       — full split requires healing both directions.

NOTE: get_local() returns *this region's own* slot only.  When checking that
a remote region's slot was merged in, always use get_global()[<remote_region>].
"""

import asyncio
import json
import time

import fakeredis
import pytest

from sync.counter import RegionalCounter
from sync.sync_service import CHANNEL, PARTITIONS, _subscribe

TIER = "free"


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _win() -> int:
    return int(time.time()) // 60


async def _make_counter(region: str) -> tuple[RegionalCounter, fakeredis.FakeAsyncRedis]:
    r = fakeredis.FakeAsyncRedis()
    return await RegionalCounter.create(r, region), r


async def _publish(
    redis, tier: str, user_id: str, window_id: int, region: str, value: int
) -> None:
    """Publish an absolute-value sync message (same format as the gateway)."""
    await redis.publish(CHANNEL, json.dumps({
        "tier": tier,
        "user_id": user_id,
        "window_id": window_id,
        "region": region,
        "value": value,
        "ts_ms": int(time.time() * 1000),
    }))


async def _cancel(*tasks) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_for(coro_factory, expected, *, timeout: float = 5.0, poll: float = 0.05) -> bool:
    """Poll `await coro_factory()` until it equals `expected` or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await coro_factory() == expected:
            return True
        await asyncio.sleep(poll)
    return False


# -----------------------------------------------------------------------
# Fixture
# -----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_partitions():
    """Ensure PARTITIONS is empty before and after each test."""
    PARTITIONS.clear()
    yield
    PARTITIONS.clear()


# -----------------------------------------------------------------------
# Test 1 — normal propagation
# -----------------------------------------------------------------------

async def test_normal_sync():
    """Messages published on redis-us propagate to eu and asia subscribers."""
    c_us, r_us = await _make_counter("us")
    c_eu, r_eu = await _make_counter("eu")
    c_asia, r_asia = await _make_counter("asia")
    uid, win = "u_normal", _win()

    t_eu = asyncio.create_task(_subscribe(r_us, c_eu, "eu", "src:us"))
    t_asia = asyncio.create_task(_subscribe(r_us, c_asia, "asia", "src:us"))
    await asyncio.sleep(0.05)

    for i in range(1, 4):  # 3 requests at gateway-us
        await c_us.increment_local(TIER, uid, win)
        await _publish(r_us, TIER, uid, win, "us", i)

    await asyncio.sleep(0.2)

    # Subscribers merge into the "us" slot of their own Redis.
    # Use get_global()["us"], not get_local() which returns the counter's own slot.
    g_eu = await c_eu.get_global(TIER, uid, win)
    g_asia = await c_asia.get_global(TIER, uid, win)
    assert g_eu["us"] == 3, f"eu.us should be 3, got {g_eu}"
    assert g_asia["us"] == 3, f"asia.us should be 3, got {g_asia}"

    await _cancel(t_eu, t_asia)


# -----------------------------------------------------------------------
# Test 2 — idempotent replay
# -----------------------------------------------------------------------

async def test_normal_sync_idempotent():
    """Replaying the same absolute value (reconciliation) does not double-count."""
    c_us, r_us = await _make_counter("us")
    c_eu, r_eu = await _make_counter("eu")
    uid, win = "u_replay", _win()

    t_eu = asyncio.create_task(_subscribe(r_us, c_eu, "eu", "src:us"))
    await asyncio.sleep(0.05)

    await c_us.increment_local(TIER, uid, win)
    await c_us.increment_local(TIER, uid, win)  # us slot = 2

    for _ in range(3):  # same absolute value published 3 times
        await _publish(r_us, TIER, uid, win, "us", 2)

    await asyncio.sleep(0.2)

    g_eu = await c_eu.get_global(TIER, uid, win)
    assert g_eu["us"] == 2, f"replay must not inflate count, got {g_eu}"

    await _cancel(t_eu)


# -----------------------------------------------------------------------
# Test 3 — partition divergence
# -----------------------------------------------------------------------

async def test_partition_divergence():
    """With (us→eu) partitioned, eu stays stale while asia receives normally."""
    c_us, r_us = await _make_counter("us")
    c_eu, r_eu = await _make_counter("eu")
    c_asia, r_asia = await _make_counter("asia")
    uid, win = "u_diverge", _win()

    t_eu = asyncio.create_task(_subscribe(r_us, c_eu, "eu", "src:us"))
    t_asia = asyncio.create_task(_subscribe(r_us, c_asia, "asia", "src:us"))
    await asyncio.sleep(0.05)

    PARTITIONS.add(("us", "eu"))  # eu drops messages from us

    for i in range(1, 5):  # 4 requests at gateway-us
        await c_us.increment_local(TIER, uid, win)
        await _publish(r_us, TIER, uid, win, "us", i)

    await asyncio.sleep(0.2)

    g_eu = await c_eu.get_global(TIER, uid, win)
    g_asia = await c_asia.get_global(TIER, uid, win)
    assert g_eu["us"] == 0, f"eu.us should be 0 (partitioned), got {g_eu}"
    assert g_asia["us"] == 4, f"asia.us should be 4, got {g_asia}"

    await _cancel(t_eu, t_asia)


# -----------------------------------------------------------------------
# Test 4 — partition is one-directional
# -----------------------------------------------------------------------

async def test_partition_is_one_way():
    """(us→eu) does not block the eu→us direction."""
    c_us, r_us = await _make_counter("us")
    c_eu, r_eu = await _make_counter("eu")
    uid, win = "u_oneway", _win()

    t_us = asyncio.create_task(_subscribe(r_eu, c_us, "us", "src:eu"))
    await asyncio.sleep(0.05)

    PARTITIONS.add(("us", "eu"))  # only us→eu blocked

    await c_eu.increment_local(TIER, uid, win)
    await _publish(r_eu, TIER, uid, win, "eu", 1)

    await asyncio.sleep(0.2)

    g_us = await c_us.get_global(TIER, uid, win)
    assert g_us["eu"] == 1, f"us should receive eu's update, got {g_us}"

    await _cancel(t_us)


# -----------------------------------------------------------------------
# Test 5 — heal convergence SLA ≤ 5 s
# -----------------------------------------------------------------------

async def test_heal_convergence():
    """After partition heals and reconciliation fires, eu converges ≤ 5 s."""
    c_us, r_us = await _make_counter("us")
    c_eu, r_eu = await _make_counter("eu")
    uid, win = "u_heal", _win()

    t_eu = asyncio.create_task(_subscribe(r_us, c_eu, "eu", "src:us"))
    await asyncio.sleep(0.05)

    # --- Phase 1: partition + accumulate 6 requests ---
    PARTITIONS.add(("us", "eu"))

    for i in range(1, 7):
        await c_us.increment_local(TIER, uid, win)
        await _publish(r_us, TIER, uid, win, "us", i)

    await asyncio.sleep(0.2)

    stale = (await c_eu.get_global(TIER, uid, win))["us"]
    assert stale == 0, f"eu.us should be stale=0 during partition, got {stale}"

    # --- Phase 2: heal + reconciliation ---
    PARTITIONS.discard(("us", "eu"))

    # Simulate _reconcile(): re-publish current absolute slot value.
    current_slot = await c_us.get_local(TIER, uid, win)  # 6
    await _publish(r_us, TIER, uid, win, "us", current_slot)

    # --- Phase 3: convergence within 5 s ---
    async def eu_us_slot():
        return (await c_eu.get_global(TIER, uid, win))["us"]

    converged = await _wait_for(eu_us_slot, expected=6, timeout=5.0)
    final = (await c_eu.get_global(TIER, uid, win))["us"]
    assert converged, f"eu.us did not converge to 6 within 5 s (final={final})"

    await _cancel(t_eu)


# -----------------------------------------------------------------------
# Test 6 — bidirectional split
# -----------------------------------------------------------------------

async def test_heal_bidirectional():
    """Full split (us↔eu) requires healing both directions to converge."""
    c_us, r_us = await _make_counter("us")
    c_eu, r_eu = await _make_counter("eu")
    uid, win = "u_bidir", _win()

    t_eu = asyncio.create_task(_subscribe(r_us, c_eu, "eu", "src:us"))
    t_us = asyncio.create_task(_subscribe(r_eu, c_us, "us", "src:eu"))
    await asyncio.sleep(0.05)

    PARTITIONS.add(("us", "eu"))
    PARTITIONS.add(("eu", "us"))

    await c_us.increment_local(TIER, uid, win)
    await _publish(r_us, TIER, uid, win, "us", 1)
    await c_eu.increment_local(TIER, uid, win)
    await _publish(r_eu, TIER, uid, win, "eu", 1)

    await asyncio.sleep(0.2)

    g_us = await c_us.get_global(TIER, uid, win)
    g_eu = await c_eu.get_global(TIER, uid, win)
    assert g_us["eu"] == 0, f"us should not see eu slot during partition, got {g_us}"
    assert g_eu["us"] == 0, f"eu should not see us slot during partition, got {g_eu}"

    # Heal both directions
    PARTITIONS.discard(("us", "eu"))
    PARTITIONS.discard(("eu", "us"))

    # Reconciliation: both regions republish their current slots
    us_slot = await c_us.get_local(TIER, uid, win)
    eu_slot = await c_eu.get_local(TIER, uid, win)
    await _publish(r_us, TIER, uid, win, "us", us_slot)
    await _publish(r_eu, TIER, uid, win, "eu", eu_slot)

    async def us_sum():
        return await c_us.global_sum(TIER, uid, win)

    async def eu_sum():
        return await c_eu.global_sum(TIER, uid, win)

    assert await _wait_for(us_sum, expected=2, timeout=5.0), \
        f"us did not converge (sum={await c_us.global_sum(TIER, uid, win)})"
    assert await _wait_for(eu_sum, expected=2, timeout=5.0), \
        f"eu did not converge (sum={await c_eu.global_sum(TIER, uid, win)})"

    await _cancel(t_eu, t_us)
