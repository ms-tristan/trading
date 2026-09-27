"""Assembly of the aggregated API application.

:func:`create_app` is the only entry point: it builds (or accepts) the engine,
stores it on ``app.state`` where the routes read it, and mounts the router.

The engine lifecycle belongs to the *lifespan* and is **opt-in** through
``start_engine``:

* ``start_engine=True`` (the production default) -- booting the application
  boots the engine: the lifespan calls ``supervisor.bootstrap()``, awaits
  ``supervisor.start()``, drives the snapshot poller while the API serves and, on
  shutdown, stops the poller and awaits ``supervisor.stop()`` so every worker is
  terminated gracefully;
* ``start_engine=False`` -- the caller owns the lifecycle and the lifespan does
  nothing. The operator CLI uses this form: it installs its own SIGTERM/SIGINT
  handlers so that a ``Ctrl-C`` reaches the supervisor exactly once, through the
  code path that also closes the state store.

A caller may pass its own ``supervisor`` (the CLI does) or let the application
build one from the platform settings, the resolved state database of
:mod:`trading_platform.paths` and the state directory beside it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .. import __version__
from ..config import PlatformSettings
from ..engine.poller import SnapshotPoller
from ..engine.supervisor import Supervisor
from ..models import utc_now
from ..paths import resolve_state_db_path, state_dir_for
from ..profiles.store import StateStore
from .routes import API_PREFIX, router

__all__ = ["API_PREFIX", "create_app"]

#: Title served by ``GET /openapi.json`` and the interactive documentation.
API_TITLE = "Trading Platform API"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot and stop the engine, or step aside when the caller owns it."""
    if not app.state.start_engine:
        yield
        return
    supervisor: Supervisor = app.state.supervisor
    supervisor.bootstrap()
    await supervisor.start()
    poller = SnapshotPoller(supervisor)
    poller_task = asyncio.create_task(poller.run_forever())
    try:
        yield
    finally:
        # Stop the clock first: a tick racing the shutdown would write a
        # snapshot of a worker the shutdown is about to terminate.
        poller.stop()
        poller_task.cancel()
        await asyncio.gather(poller_task, return_exceptions=True)
        await supervisor.stop()


def create_app(
    *,
    supervisor: Supervisor | None = None,
    settings: PlatformSettings | None = None,
    state_dir: Path | None = None,
    start_engine: bool = True,
) -> FastAPI:
    """Build the aggregated API application.

    ``supervisor`` defaults to a real engine over the resolved state database;
    ``settings`` defaults to the settings of ``config/platform.json`` merged with
    the ``TB_*`` overrides; ``state_dir`` defaults to the directory of that
    database (it is where the kill-switch file and the worker logs live).

    ``start_engine=False`` keeps the lifespan inert so a test -- or the CLI, which
    owns the signals -- can drive the engine itself.
    """
    resolved_settings = PlatformSettings.load() if settings is None else settings
    engine = supervisor
    if engine is None:
        state_db = resolve_state_db_path()
        resolved_state_dir = state_dir_for(state_db) if state_dir is None else Path(state_dir)
        engine = Supervisor(
            store=StateStore(state_db),
            settings=resolved_settings,
            state_dir=resolved_state_dir,
        )
    else:
        resolved_state_dir = Path(state_dir) if state_dir is not None else Path(engine.state_dir)

    app = FastAPI(title=API_TITLE, version=__version__, lifespan=_lifespan)
    app.state.supervisor = engine
    app.state.settings = getattr(engine, "settings", resolved_settings)
    app.state.state_dir = resolved_state_dir
    app.state.started_at = utc_now()
    app.state.start_engine = bool(start_engine)
    app.include_router(router)
    return app
