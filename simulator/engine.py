import asyncio
import dataclasses
import math
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import httpx
import numpy as np

from population import pick_user, pick_endpoint

if TYPE_CHECKING:
    from stats import LiveStats

GATEWAY_URLS: dict[str, str] = {
    "us":   "http://localhost:8081",
    "eu":   "http://localhost:8082",
    "asia": "http://localhost:8083",
}


@dataclasses.dataclass
class RequestSpec:
    user_id:  str
    tier:     str
    region:   str
    endpoint: str

    def to_payload(self) -> dict:
        return {
            "user_id":  self.user_id,
            "tier":     self.tier,
            "region":   self.region,
            "endpoint": self.endpoint,
        }


def diurnal_factor(elapsed_s: float) -> float:
    """Compress a 24-hour sine curve into a 5-minute window. Range: [0.5, 1.5]."""
    return 1.0 + 0.5 * math.sin(2 * math.pi * elapsed_s / 300)


async def poisson_stream(
    rps: float,
    duration: float,
    population: dict[str, list[str]],
    region: str,
    *,
    diurnal: bool = False,
    bias_user: str | None = None,
    bias_share: float = 0.0,
    bias_users: list[tuple[str, float]] | None = None,
) -> AsyncGenerator[RequestSpec, None]:
    """Yield RequestSpecs at Poisson-distributed intervals until duration elapses."""
    deadline = time.monotonic() + duration
    t0 = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        effective_rps = rps * diurnal_factor(now - t0) if diurnal else rps
        effective_rps = max(effective_rps, 0.1)
        delay = np.random.exponential(1.0 / effective_rps)
        # Cap sleep to remaining duration so we don't overshoot
        remaining = deadline - time.monotonic()
        await asyncio.sleep(min(delay, max(remaining, 0)))
        if time.monotonic() >= deadline:
            break
        user_id, tier = pick_user(
            population,
            bias_user=bias_user,
            bias_share=bias_share,
            bias_users=bias_users,
        )
        yield RequestSpec(
            user_id=user_id,
            tier=tier,
            region=region,
            endpoint=pick_endpoint(),
        )


async def send_one(
    spec: RequestSpec,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    stats: "LiveStats",
) -> None:
    async with semaphore:
        for attempt in range(3):
            t_start = time.monotonic()
            try:
                resp = await client.post(
                    GATEWAY_URLS[spec.region] + "/check",
                    json=spec.to_payload(),
                )
                latency_ms = (time.monotonic() - t_start) * 1000
                if resp.status_code == 400:
                    # Gateway rejected request (e.g. region mismatch) — don't retry
                    await stats.record_error()
                    return
                await stats.record(
                    resp.json(),
                    latency_ms,
                    tier=spec.tier,
                    region=spec.region,
                    user_id=spec.user_id,
                )
                return
            except httpx.ConnectError:
                if attempt == 2:
                    await stats.record_error()
                    return
                await asyncio.sleep(0.1 * (2 ** attempt))
            except (httpx.TimeoutException, httpx.RemoteProtocolError):
                if attempt == 2:
                    await stats.record_error()
                    return
                await asyncio.sleep(0.1 * (2 ** attempt))


async def run(
    stream_gen: AsyncGenerator[RequestSpec, None],
    stats: "LiveStats",
    *,
    concurrency: int = 50,
    client: httpx.AsyncClient | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> None:
    """Drive stream_gen through send_one with bounded concurrency."""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    if semaphore is None:
        semaphore = asyncio.Semaphore(concurrency)

    tasks: list[asyncio.Task] = []
    try:
        async for spec in stream_gen:
            task = asyncio.create_task(send_one(spec, client, semaphore, stats))
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)
    finally:
        if own_client:
            await client.aclose()
