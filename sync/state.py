"""Shared mutable state for the sync service.

Kept in a separate module so admin.py can import PARTITIONS without
triggering a re-import of sync_service.py (which would cause duplicate
Prometheus metric registrations when running as __main__).
"""

from __future__ import annotations

# A tuple (from_region, to_region) present here causes _subscribe() to
# silently drop messages from from_region before applying max-merge.
PARTITIONS: set[tuple[str, str]] = set()
