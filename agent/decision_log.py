"""
JSONL decision log — one entry per agent tick.

Each entry captures the full observable state (features, predictions,
anomaly flags, decisions taken) so the eval notebook can reconstruct
the agent's reasoning at any point without re-running anything.

The file is line-buffered so crash-safe: no entry is lost mid-write
because newlines flush the OS buffer.
"""

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


class DecisionLog:
    def __init__(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # buffering=1 → line-buffered: each \n write is an OS flush
        self._fh = open(path, "a", buffering=1, encoding="utf-8")
        log.info("Decision log: %s", os.path.abspath(path))

    def append(self, entry: dict[str, Any]) -> None:
        try:
            self._fh.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            log.error("decision log write failed: %s", exc)

    def flush(self) -> None:
        try:
            self._fh.flush()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass
