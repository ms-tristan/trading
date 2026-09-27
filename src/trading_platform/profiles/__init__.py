"""Profile catalogue discovery and the SQLite state store.

* :mod:`trading_platform.profiles.catalogue` -- what the platform offers: the
  strategies of ``config/strategies.json`` plus the files discovered in
  ``user_data/strategies``, and the declarative profiles of ``config/profiles.json``;
* :mod:`trading_platform.profiles.store` -- what the platform knows at run time:
  the profiles, their minute snapshots, the settings and the event journal, and
  the archive of a foreign ``state.db`` left by the previous product.
"""

from __future__ import annotations

from .catalogue import (
    StrategyCatalogue,
    discover_strategy_files,
    load_profile_catalogue,
    load_strategy_catalogue,
)
from .store import SCHEMA_VERSION, TABLES, StateStore, archive_legacy_database

__all__ = [
    "SCHEMA_VERSION",
    "TABLES",
    "StateStore",
    "StrategyCatalogue",
    "archive_legacy_database",
    "discover_strategy_files",
    "load_profile_catalogue",
    "load_strategy_catalogue",
]
