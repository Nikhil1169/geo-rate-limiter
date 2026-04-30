import asyncio
import sys

import click

from engine import run, poisson_stream, GATEWAY_URLS
from population import load_population
from scenarios import run_scenario, list_scenarios
from stats import LiveStats, periodic_print, final_summary, write_csv


def _run(coro):
    try:
        asyncio.run(coro)
    except KeyboardInterrupt:
        click.echo("\n[interrupted]")
        sys.exit(0)


def _parse_weights(_ctx, _param, value: str) -> dict[str, float]:
    result: dict[str, float] = {}
    try:
        for part in value.split(","):
            region, w = part.strip().split(":")
            if region not in GATEWAY_URLS:
                raise click.BadParameter(f"unknown region '{region}'")
            result[region] = float(w)
    except (ValueError, AttributeError):
        raise click.BadParameter("format must be 'us:0.5,eu:0.3,asia:0.2'")
    total = sum(result.values())
    if abs(total - 1.0) > 0.01:
        raise click.BadParameter(f"weights must sum to 1.0, got {total:.3f}")
    return result


@click.group()
def simulate() -> None:
    """Geo-distributed rate limiter traffic simulator."""


# ---------------------------------------------------------------------------
# steady
# ---------------------------------------------------------------------------

@simulate.command()
@click.option("--rps",         required=True,  type=float,                           help="Target requests per second")
@click.option("--duration",    required=True,  type=float,                           help="Run duration in seconds")
@click.option("--region",      required=True,  type=click.Choice(["us","eu","asia"]), help="Target region")
@click.option("--diurnal",     is_flag=True,   default=False,                        help="Overlay sine-wave diurnal pattern")
@click.option("--concurrency", default=50,     type=int,                             help="Max concurrent in-flight requests")
@click.option("--output",      default=None,   type=str,                             help="Write CSV results to this path")
def steady(rps, duration, region, diurnal, concurrency, output):
    """Constant Poisson-distributed load against one region."""
    population = load_population()
    stats = LiveStats()

    async def _go():
        printer = asyncio.create_task(periodic_print(stats))
        stream = poisson_stream(rps, duration, population, region, diurnal=diurnal)
        await run(stream, stats, concurrency=concurrency)
        printer.cancel()

    _run(_go())
    final_summary(stats)
    if output:
        write_csv(stats, output)
        click.echo(f"Results written to {output}")


# ---------------------------------------------------------------------------
# spike
# ---------------------------------------------------------------------------

@simulate.command()
@click.option("--base",        required=True,  type=float,                           help="Base RPS before spike")
@click.option("--peak",        required=True,  type=float,                           help="RPS during spike")
@click.option("--at",          required=True,  type=float,                           help="Seconds into run when spike starts")
@click.option("--duration",    required=True,  type=float,                           help="Total run duration in seconds")
@click.option("--region",      required=True,  type=click.Choice(["us","eu","asia"]), help="Target region")
@click.option("--concurrency", default=50,     type=int,                             help="Max concurrent in-flight requests")
@click.option("--output",      default=None,   type=str,                             help="Write CSV results to this path")
def spike(base, peak, at, duration, region, concurrency, output):
    """Run at base RPS, then ramp to peak RPS at --at seconds."""
    if at >= duration:
        raise click.BadParameter("--at must be less than --duration")
    population = load_population()
    stats = LiveStats()

    async def _spike_stream():
        async for spec in poisson_stream(base, at, population, region):
            yield spec
        async for spec in poisson_stream(peak, duration - at, population, region):
            yield spec

    async def _go():
        printer = asyncio.create_task(periodic_print(stats))
        await run(_spike_stream(), stats, concurrency=concurrency)
        printer.cancel()

    _run(_go())
    final_summary(stats)
    if output:
        write_csv(stats, output)
        click.echo(f"Results written to {output}")


# ---------------------------------------------------------------------------
# noisy
# ---------------------------------------------------------------------------

@simulate.command()
@click.option("--culprit",     required=True,  type=str,                             help="User ID of the noisy neighbor (must be in free tier)")
@click.option("--share",       required=True,  type=float,                           help="Fraction 0.0-1.0 of requests from culprit")
@click.option("--duration",    required=True,  type=float,                           help="Run duration in seconds")
@click.option("--region",      default="us",   type=click.Choice(["us","eu","asia"]), help="Target region (default: us)")
@click.option("--rps",         default=20.0,   type=float,                           help="Total RPS (default: 20)")
@click.option("--concurrency", default=50,     type=int,                             help="Max concurrent in-flight requests")
@click.option("--output",      default=None,   type=str,                             help="Write CSV results to this path")
def noisy(culprit, share, duration, region, rps, concurrency, output):
    """Simulate a noisy neighbor: one user sends a disproportionate share of traffic."""
    if not (0.0 < share <= 1.0):
        raise click.BadParameter("--share must be between 0.0 and 1.0 (exclusive)")
    population = load_population()
    if culprit not in population.get("free", []):
        raise click.BadParameter(f"--culprit '{culprit}' not found in free-tier population")

    stats = LiveStats()

    async def _go():
        printer = asyncio.create_task(periodic_print(stats))
        stream = poisson_stream(rps, duration, population, region,
                                bias_user=culprit, bias_share=share)
        await run(stream, stats, concurrency=concurrency)
        printer.cancel()

    _run(_go())
    final_summary(stats)
    if output:
        write_csv(stats, output)
        click.echo(f"Results written to {output}")


# ---------------------------------------------------------------------------
# distribution
# ---------------------------------------------------------------------------

@simulate.command()
@click.option("--weights",     required=True,  type=str,  callback=_parse_weights,   help="Region weights e.g. us:0.5,eu:0.3,asia:0.2 (must sum to 1.0)")
@click.option("--rps",         required=True,  type=float,                           help="Total RPS across all regions")
@click.option("--duration",    required=True,  type=float,                           help="Run duration in seconds")
@click.option("--diurnal",     is_flag=True,   default=False,                        help="Overlay sine-wave diurnal pattern")
@click.option("--concurrency", default=50,     type=int,                             help="Max concurrent in-flight requests")
@click.option("--output",      default=None,   type=str,                             help="Write CSV results to this path")
def distribution(weights, rps, duration, diurnal, concurrency, output):
    """Distribute traffic across regions by weight, sharing a single stats instance."""
    population = load_population()
    stats = LiveStats()

    async def _go():
        import httpx as _httpx
        printer = asyncio.create_task(periodic_print(stats))
        semaphore = asyncio.Semaphore(concurrency)
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(5.0)) as client:
            streams = [
                poisson_stream(rps * w, duration, population, region, diurnal=diurnal)
                for region, w in weights.items()
            ]
            await asyncio.gather(*[
                run(s, stats, concurrency=concurrency, client=client, semaphore=semaphore)
                for s in streams
            ])
        printer.cancel()

    _run(_go())
    final_summary(stats)
    if output:
        write_csv(stats, output)
        click.echo(f"Results written to {output}")


# ---------------------------------------------------------------------------
# scenario
# ---------------------------------------------------------------------------

@simulate.command()
@click.argument("name")
@click.option("--concurrency", default=50, type=int, help="Max concurrent in-flight requests")
@click.option("--output",      default=None, type=str, help="Write CSV results to this path")
def scenario(name, concurrency, output):
    """Run a predefined scenario. Available: """ + ", ".join(list_scenarios())
    stats = LiveStats()

    async def _go():
        printer = asyncio.create_task(periodic_print(stats))
        try:
            await run_scenario(name, stats, concurrency=concurrency)
        except KeyError as e:
            printer.cancel()
            raise click.ClickException(str(e))
        printer.cancel()

    _run(_go())
    final_summary(stats)
    if output:
        write_csv(stats, output)
        click.echo(f"Results written to {output}")


if __name__ == "__main__":
    simulate()
