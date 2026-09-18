"""Web layer (layer 7): the standard-library monitoring surface.

Public surface
--------------
* :class:`~trading_platform.web.routes.Router` -- the pure request/response
  mapping (no socket, no thread), and the whole JSON contract of the monitoring
  API;
* :class:`~trading_platform.web.routes.SnapshotProvider` -- the *only* seam the
  web layer knows about the running platform, so the orchestrator is never
  imported here and persisted state can be served with no engine running;
* :class:`~trading_platform.web.server.MonitoringServer` plus
  :func:`~trading_platform.web.server.create_server`,
  :func:`~trading_platform.web.server.serve` and
  :func:`~trading_platform.web.server.start_in_thread` -- the HTTP transport;
* :func:`~trading_platform.web.routes.operator_token_from_env` -- the single
  operator token of the mutating route, resolved from the environment and never
  logged.

Every import is eager on purpose: the layer is standard-library only (no optional
extra, no ``ccxt``, no ``freqtrade``), so importing it can never fail because an
optional dependency is missing.
"""

from __future__ import annotations

from trading_platform.web.routes import Router, SnapshotProvider, operator_token_from_env
from trading_platform.web.server import (
    MonitoringServer,
    create_server,
    serve,
    start_in_thread,
)

__all__ = [
    "MonitoringServer",
    "Router",
    "SnapshotProvider",
    "create_server",
    "operator_token_from_env",
    "serve",
    "start_in_thread",
]
