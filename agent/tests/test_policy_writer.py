"""
Tests for policy_writer: Contract 4 JSON shape and Redis writes.

Uses fakeredis for async Redis operations — no real Redis required.
"""

import json
import re

import fakeredis.aioredis as aio_fakeredis
import pytest

from agent.decider import Decision
from agent.policy_writer import _make_override, _make_policy, apply_decisions

# ── Contract 4 required fields ────────────────────────────────────────────────

_POLICY_FIELDS = frozenset({
    "policy_id", "region", "tier", "limit_per_minute",
    "burst", "algorithm", "ttl_seconds", "reason", "created_at",
})
_POLICY_ID_RE = re.compile(r"^pol_\d+_\d+$")


# ── fixtures and helpers ──────────────────────────────────────────────────────

def _policy_decision(**kw) -> Decision:
    return Decision(
        type="policy",
        region=kw.get("region", "us"),
        tier=kw.get("tier", "free"),
        user_id=None,
        limit_per_minute=kw.get("limit_per_minute", 300),
        ttl=kw.get("ttl", 300),
        reason=kw.get("reason", "test_policy"),
    )


def _override_decision(**kw) -> Decision:
    return Decision(
        type="override",
        region=kw.get("region", "us"),
        tier=kw.get("tier", "free"),
        user_id=kw.get("user_id", "free_00001"),
        limit_per_minute=kw.get("limit_per_minute", 30),
        ttl=kw.get("ttl", 300),
        reason=kw.get("reason", "noisy_neighbor_free_00001"),
    )


@pytest.fixture
def fake_redis_pool():
    r = aio_fakeredis.FakeRedis(decode_responses=True)
    return {"us": r, "eu": r, "asia": r}


# ── Policy payload (Contract 4) ───────────────────────────────────────────────

class TestPolicyPayload:
    def test_all_contract4_fields_present(self):
        payload = _make_policy(_policy_decision())
        missing = _POLICY_FIELDS - set(payload.keys())
        assert not missing, f"Missing fields: {missing}"

    def test_no_extra_unexpected_fields(self):
        payload = _make_policy(_policy_decision())
        extra = set(payload.keys()) - _POLICY_FIELDS
        assert not extra, f"Unexpected extra fields: {extra}"

    def test_policy_id_format(self):
        payload = _make_policy(_policy_decision())
        assert _POLICY_ID_RE.match(payload["policy_id"]), (
            f"policy_id {payload['policy_id']!r} must match pol_{{unix_ts}}_{{seq}}"
        )

    def test_policy_id_seq_increments(self):
        d = _policy_decision()
        p1 = _make_policy(d)
        p2 = _make_policy(d)
        seq1 = int(p1["policy_id"].rsplit("_", 1)[-1])
        seq2 = int(p2["policy_id"].rsplit("_", 1)[-1])
        assert seq2 == seq1 + 1

    def test_algorithm_is_token_bucket(self):
        payload = _make_policy(_policy_decision())
        assert payload["algorithm"] == "token_bucket"

    def test_burst_is_one_fifth_of_limit(self):
        payload = _make_policy(_policy_decision(limit_per_minute=300))
        assert payload["burst"] == max(1, 300 // 5)  # 60

    def test_burst_minimum_is_one(self):
        payload = _make_policy(_policy_decision(limit_per_minute=1))
        assert payload["burst"] == 1

    def test_ttl_seconds_field_name(self):
        """Contract 4 uses 'ttl_seconds' (not 'ttl') for policies."""
        payload = _make_policy(_policy_decision(ttl=300))
        assert "ttl_seconds" in payload
        assert "ttl" not in payload or payload.get("ttl_seconds") == 300

    def test_region_and_tier_propagated(self):
        payload = _make_policy(_policy_decision(region="eu", tier="premium", limit_per_minute=3000))
        assert payload["region"] == "eu"
        assert payload["tier"] == "premium"
        assert payload["limit_per_minute"] == 3000


# ── Override payload ──────────────────────────────────────────────────────────

class TestOverridePayload:
    def test_override_uses_ttl_not_ttl_seconds(self):
        """Override schema must use 'ttl', not 'ttl_seconds' (gateway parses .TTL)."""
        payload = _make_override(_override_decision())
        assert "ttl" in payload, "Override must have 'ttl' field"
        assert "ttl_seconds" not in payload, "Override must NOT have 'ttl_seconds'"

    def test_override_has_exactly_three_fields(self):
        payload = _make_override(_override_decision())
        assert set(payload.keys()) == {"limit_per_minute", "ttl", "reason"}

    def test_override_ttl_value(self):
        payload = _make_override(_override_decision(ttl=300))
        assert payload["ttl"] == 300

    def test_override_limit_propagated(self):
        payload = _make_override(_override_decision(limit_per_minute=30))
        assert payload["limit_per_minute"] == 30


# ── Redis integration (async, fakeredis) ──────────────────────────────────────

class TestApplyDecisions:
    async def test_policy_written_to_correct_key(self, fake_redis_pool):
        d = _policy_decision(region="us", tier="free", limit_per_minute=210)
        await apply_decisions([d], fake_redis_pool)

        raw = await fake_redis_pool["us"].get("policy:us:free")
        assert raw is not None, "policy:us:free should be set in Redis"
        payload = json.loads(raw)
        assert payload["limit_per_minute"] == 210
        assert _POLICY_ID_RE.match(payload["policy_id"])

    async def test_policy_round_trips_all_contract4_fields(self, fake_redis_pool):
        d = _policy_decision(region="eu", tier="premium", limit_per_minute=3300, ttl=300)
        await apply_decisions([d], fake_redis_pool)

        raw = await fake_redis_pool["eu"].get("policy:eu:premium")
        payload = json.loads(raw)
        missing = _POLICY_FIELDS - set(payload.keys())
        assert not missing, f"Round-trip missing fields: {missing}"

    async def test_override_written_with_ttl_field(self, fake_redis_pool):
        d = _override_decision(user_id="free_00001", limit_per_minute=30)
        await apply_decisions([d], fake_redis_pool)

        raw = await fake_redis_pool["us"].get("override:free_00001")
        assert raw is not None
        payload = json.loads(raw)
        assert "ttl" in payload
        assert "ttl_seconds" not in payload
        assert payload["limit_per_minute"] == 30

    async def test_multiple_decisions_all_written(self, fake_redis_pool):
        decisions = [
            _policy_decision(region="us", tier="free", limit_per_minute=210),
            _policy_decision(region="us", tier="premium", limit_per_minute=3300),
        ]
        written = await apply_decisions(decisions, fake_redis_pool)
        assert len(written) == 2

        for key, limit in [("policy:us:free", 210), ("policy:us:premium", 3300)]:
            raw = await fake_redis_pool["us"].get(key)
            assert raw is not None
            assert json.loads(raw)["limit_per_minute"] == limit

    async def test_apply_returns_written_payloads(self, fake_redis_pool):
        d = _policy_decision(region="us", tier="free", limit_per_minute=300)
        written = await apply_decisions([d], fake_redis_pool)
        assert len(written) == 1
        assert written[0]["limit_per_minute"] == 300
        assert "policy_id" in written[0]
