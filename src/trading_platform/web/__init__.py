"""Web layer (layer 7): the standard-library monitoring **JSON API**.

The layer serves no HTML document and no static asset: every route answers
JSON, and the dashboard is a separate application (``dashboard/``, a Next.js
server) that consumes this API from its own origin.  ``GET /`` and every
``/static/...`` path are therefore unknown routes and answer the documented
JSON ``404``.

Public surface
--------------
* :class:`~trading_platform.web.routes.Router` -- the pure request/response
  mapping (no socket, no thread), and the whole JSON contract of the monitoring
  API;
* :class:`~trading_platform.web.routes.SnapshotProvider` -- the *only* seam the
  web layer knows about the running platform, so the orchestrator is never
  imported here and persisted state can be served with no engine running;
* :class:`~trading_platform.web.routes.CatalogProvider` -- the four vocabularies
  of the dashboard pickers (symbols, strategies, timeframes, modes), backed by a
  static fallback so ``GET /api/catalog`` never answers a ``500``;
* :class:`~trading_platform.web.routes.ProfileController` -- runtime control of
  the running engine (pause, resume, delete, create, and the pause state of every
  profile) through the injected thread/event-loop bridge; an absent controller
  keeps the read surface working and refuses every lifecycle route with the
  documented ``403``;
* :class:`~trading_platform.web.server.MonitoringServer` plus
  :func:`~trading_platform.web.server.create_server`,
  :func:`~trading_platform.web.server.serve` and
  :func:`~trading_platform.web.server.start_in_thread` -- the HTTP transport;
* :func:`~trading_platform.web.routes.operator_token_from_env` -- the single
  operator token of the mutating routes, resolved from the environment and never
  logged.

Every import is eager on purpose: the layer is standard-library only (no optional
extra, no ``ccxt``, no ``freqtrade``), so importing it can never fail because an
optional dependency is missing.
"""

from __future__ import annotations

from trading_platform.web.routes import (
    CatalogProvider,
    ProfileController,
    Router,
    SnapshotProvider,
    operator_token_from_env,
)
from trading_platform.web.server import (
    MonitoringServer,
    create_server,
    serve,
    start_in_thread,
)

__all__ = [
    "CatalogProvider",
    "MonitoringServer",
    "ProfileController",
    "Router",
    "SnapshotProvider",
    "create_server",
    "operator_token_from_env",
    "serve",
    "start_in_thread",
]
