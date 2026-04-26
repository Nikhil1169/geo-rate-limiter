"""RegionalCounter — G-Counter CRDT backed by a single Redis instance.

Each region owns exactly one slot in the hash
  rl:global:{tier}:{user_id}:{window_id}
and only ever increments that slot.  Merging remote state uses max-per-slot
(merge.lua), which is associative, commutative, and idempotent.
"""
from __future__ import annotations

import pathlib

REGIONS: tuple[str, ...] = ("us", "eu", "asia")
_LUA_PATH = pathlib.Path(__file__).parent / "merge.lua"
_TTL = 120  # seconds — one extra window of grace for late arrivals


def _key(tier: str, user_id: str, window_id: int) -> str:
    return f"rl:global:{tier}:{user_id}:{window_id}"


class RegionalCounter:
    """Async wrapper around a region's Redis instance for G-Counter ops.

    Use `await RegionalCounter.create(redis, region)` — never instantiate
    directly, because the Lua script must be loaded first.
    """

    def __init__(self, redis, region: str, sha: str) -> None:
        self._r = redis
        self.region = region
        self._sha = sha  # EVALSHA handle for merge.lua

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def create(cls, redis, region: str) -> "RegionalCounter":
        """Load merge.lua into Redis and return a ready counter."""
        sha: str = await redis.script_load(_LUA_PATH.read_text())
        return cls(redis, region, sha)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def increment_local(self, tier: str, user_id: str, window_id: int) -> int:
        """Increment this region's slot by 1.  Returns the new slot value."""
        key = _key(tier, user_id, window_id)
        new_val = await self._r.hincrby(key, self.region, 1)
        await self._r.expire(key, _TTL)
        return int(new_val)

    async def merge_remote(
        self,
        tier: str,
        user_id: str,
        window_id: int,
        remote_region: str,
        remote_value: int,
    ) -> int:
        """Apply max(current_slot, remote_value) for remote_region's slot.

        Runs merge.lua atomically.  Safe to call with duplicate or
        out-of-order messages — the Lua script is a no-op when the stored
        value is already >= remote_value.
        """
        key = _key(tier, user_id, window_id)
        return int(
            await self._r.evalsha(
                self._sha,
                1,        # numkeys
                key,      # KEYS[1]
                remote_region,          # ARGV[1]
                str(remote_value),      # ARGV[2]
                str(_TTL),              # ARGV[3]
            )
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_local(self, tier: str, user_id: str, window_id: int) -> int:
        """Return this region's current slot value (0 if key absent)."""
        raw = await self._r.hget(_key(tier, user_id, window_id), self.region)
        return int(raw) if raw is not None else 0

    async def get_global(
        self, tier: str, user_id: str, window_id: int
    ) -> dict[str, int]:
        """Return all slot values as {region: count}.  Missing slots are 0."""
        raw: dict = await self._r.hgetall(_key(tier, user_id, window_id))
        out: dict[str, int] = {r: 0 for r in REGIONS}
        for field, val in raw.items():
            name = field.decode() if isinstance(field, bytes) else str(field)
            if name in out:
                out[name] = int(val)
        return out

    async def global_sum(self, tier: str, user_id: str, window_id: int) -> int:
        """Convenience: sum of all slots (the user's global request count)."""
        return sum((await self.get_global(tier, user_id, window_id)).values())
