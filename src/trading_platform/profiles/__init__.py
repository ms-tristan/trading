"""Declarative profile catalogue and its idempotent apply path.

The package answers one question: *which profiles should the platform hold?*  It
is deliberately split in two halves, and the split is the whole design:

:mod:`trading_platform.profiles.catalogue`
    the **declaration** -- a frozen, typed, pure table of
    :class:`~trading_platform.profiles.catalogue.ProfileDefinition` values (26
    paper profiles spread over the ten house strategies, plus one live entry).
    It imports no I/O, opens no socket, reads no clock and reads no environment:
    every number it publishes is derived from the strategy registry and from
    :mod:`trading_platform.realtime.warmup`, the platform's single arithmetic
    authority, so the catalogue can never drift from what the strategies really
    need;
:mod:`trading_platform.profiles.apply`
    the **reconciliation** -- an idempotent, dry-runnable, non-destructive apply
    path that reads the running platform over its own HTTP API, creates what is
    missing, never deletes anything without ``--prune``, and refuses to commit
    more paper capital than the shared ledger holds.

Layer direction (frozen)
------------------------
``profiles`` is a **new top-level package** that sits *outside* the layering of
``core``/``config``/``data``/``strategy``/``validation``/``realtime``/``web``:
it consumes ``config``, ``strategy`` and the pure seams of ``realtime``
(``warmup``, ``credentials``, ``risk``) and it is consumed by the CLI only.  It
must therefore never be imported by ``trading_platform.strategy`` nor by
``trading_platform.realtime``, and ``trading_platform.cli`` imports it **inside
the command body** -- importing the CLI must stay as cheap as it is today.

The package is import-light on purpose: no network client is built at import
time (``urllib`` is reached by :mod:`trading_platform.profiles.apply` only when a
request is actually sent), so ``python -c "import trading_platform.profiles"``
never touches a socket.
"""

from __future__ import annotations

from trading_platform.profiles.apply import (
    PROVISION_COMMAND,
    ApplyPlan,
    ProfileApiClient,
    apply_catalogue,
    ledger_capacity,
    live_credential_issues,
    plan_apply,
)
from trading_platform.profiles.catalogue import (
    CATALOGUE_BY_ID,
    HISTORY_HEADROOM_CANDLES,
    LIVE_PROFILE_INITIAL_BALANCE,
    PAPER_PROFILE_INITIAL_BALANCE,
    PROFILE_CATALOGUE,
    ProfileDefinition,
    live_definitions,
    missing_definitions,
    paper_definitions,
    paper_total_initial_balance,
    prunable_profile_ids,
    required_warmup_candles,
)

__all__ = [
    "CATALOGUE_BY_ID",
    "HISTORY_HEADROOM_CANDLES",
    "LIVE_PROFILE_INITIAL_BALANCE",
    "PAPER_PROFILE_INITIAL_BALANCE",
    "PROFILE_CATALOGUE",
    "PROVISION_COMMAND",
    "ApplyPlan",
    "ProfileApiClient",
    "ProfileDefinition",
    "apply_catalogue",
    "ledger_capacity",
    "live_credential_issues",
    "live_definitions",
    "missing_definitions",
    "paper_definitions",
    "paper_total_initial_balance",
    "plan_apply",
    "prunable_profile_ids",
    "required_warmup_candles",
]
