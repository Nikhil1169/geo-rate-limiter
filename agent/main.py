"""
AI traffic agent — CLI entrypoint.

Usage:
    python -m agent.main run [--predictor ewma|holtwinters] [--interval 15]

Config is also read from environment variables (see agent/config.py).
"""

import asyncio
import logging
import os

import click

from agent.config import load_config
from agent.loop import run


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@click.group()
def cli() -> None:
    """Geo-distributed rate limiter — AI traffic agent."""


@cli.command()
@click.option(
    "--predictor",
    type=click.Choice(["ewma", "holtwinters"]),
    default=None,
    help="Forecasting algorithm (overrides PREDICTOR env var).",
)
@click.option(
    "--interval",
    "interval_seconds",
    type=int,
    default=None,
    help="Tick interval in seconds (overrides INTERVAL_SECONDS env var).",
)
def run_cmd(predictor: str | None, interval_seconds: int | None) -> None:
    """Run the agent loop."""
    _configure_logging()
    cfg = load_config()

    # CLI flags override env vars
    if predictor is not None:
        os.environ["PREDICTOR"] = predictor
        cfg = load_config()
    if interval_seconds is not None:
        os.environ["INTERVAL_SECONDS"] = str(interval_seconds)
        cfg = load_config()

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


# Register as `run` subcommand (matches `python -m agent.main run`)
cli.add_command(run_cmd, name="run")


if __name__ == "__main__":
    cli()
