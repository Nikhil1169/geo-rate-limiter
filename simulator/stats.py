import asyncio
import collections
import csv
import dataclasses
import time


@dataclasses.dataclass
class Sample:
    ts:         float   # seconds since run start
    latency_ms: float
    allowed:    bool
    tier:       str
    region:     str


class LiveStats:
    def __init__(self) -> None:
        self._lock     = asyncio.Lock()
        self.sent      = 0
        self.allowed   = 0
        self.denied    = 0
        self.errors    = 0
        self._samples: collections.deque[Sample] = collections.deque(maxlen=10_000)
        self._start_ts = time.monotonic()

    async def record(
        self,
        response: dict,
        latency_ms: float,
        *,
        tier: str,
        region: str,
    ) -> None:
        async with self._lock:
            self.sent += 1
            if response.get("allowed"):
                self.allowed += 1
            else:
                self.denied += 1
            self._samples.append(Sample(
                ts=time.monotonic() - self._start_ts,
                latency_ms=latency_ms,
                allowed=bool(response.get("allowed")),
                tier=tier,
                region=region,
            ))

    async def record_error(self) -> None:
        async with self._lock:
            self.sent += 1
            self.errors += 1

    async def snapshot(self) -> dict:
        async with self._lock:
            elapsed = time.monotonic() - self._start_ts
            return {
                "sent":    self.sent,
                "allowed": self.allowed,
                "denied":  self.denied,
                "errors":  self.errors,
                "elapsed": elapsed,
                "rps":     self.sent / elapsed if elapsed > 0 else 0.0,
            }


async def periodic_print(stats: LiveStats, interval: float = 5.0) -> None:
    """Print a live stats line every interval seconds. Cancelled when run completes."""
    while True:
        await asyncio.sleep(interval)
        snap = await stats.snapshot()
        elapsed = int(snap["elapsed"])
        print(
            f"[t+{elapsed}s] "
            f"sent={snap['sent']} "
            f"allowed={snap['allowed']} "
            f"denied={snap['denied']} "
            f"errors={snap['errors']} "
            f"rps={snap['rps']:.1f}"
        )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = (len(sorted_v) - 1) * pct / 100
    lo = int(idx)
    hi = min(lo + 1, len(sorted_v) - 1)
    frac = idx - lo
    return sorted_v[lo] + frac * (sorted_v[hi] - sorted_v[lo])


def final_summary(stats: LiveStats) -> None:
    elapsed = time.monotonic() - stats._start_ts
    sent    = stats.sent
    allowed = stats.allowed
    denied  = stats.denied
    errors  = stats.errors

    latencies = [s.latency_ms for s in stats._samples]
    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)

    denied_by_tier: dict[str, int] = {}
    for s in stats._samples:
        if not s.allowed:
            denied_by_tier[s.tier] = denied_by_tier.get(s.tier, 0) + 1

    def pct(n: int) -> str:
        return f"({n / sent * 100:.1f}%)" if sent > 0 else "(0.0%)"

    bar = "=" * 40
    sep = "-" * 40
    print(f"\n{bar}")
    print(f" Run complete — {elapsed:.1f} s")
    print(sep)
    print(f" Sent    : {sent}")
    print(f" Allowed : {allowed:<7} {pct(allowed)}")
    print(f" Denied  : {denied:<7} {pct(denied)}")
    print(f" Errors  : {errors:<7} {pct(errors)}")
    print()
    print(" Latency (ms)")
    print(f"   p50  : {p50:6.1f}")
    print(f"   p95  : {p95:6.1f}")
    print(f"   p99  : {p99:6.1f}")
    print()
    print(" Denied by tier")
    for tier in ("free", "premium", "internal"):
        count = denied_by_tier.get(tier, 0)
        print(f"   {tier:<10}: {count}")
    print(bar)


def write_csv(stats: LiveStats, path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "latency_ms", "allowed", "tier", "region"])
        for s in stats._samples:
            writer.writerow([
                f"{s.ts:.3f}",
                f"{s.latency_ms:.2f}",
                "true" if s.allowed else "false",
                s.tier,
                s.region,
            ])
