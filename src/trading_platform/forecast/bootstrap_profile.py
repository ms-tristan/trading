"""Build the forecast artifact a realtime profile declares, fully offline.

A forecast-driven profile is only usable once an artifact exists *for it*: the
symbol, the timeframe and the seasonal period of the artifact must match the
profile, and its coverage must still reach the profile's decision horizon (see
:func:`trading_platform.realtime.features.check_profile_forecast`).  Producing
that artifact by hand means re-typing three declarations -- the symbol, the
timeframe and the candle file -- and any typo produces a profile that starts and
then never trades, which is exactly the failure the startup guard exists to
prevent.

:func:`bootstrap_profile_forecast` is the repeatable answer: point it at a
profiles file, name a profile, and it reads that profile, finds the candle file
the profile's own declarations imply, builds the artifact and writes it where
``docs/realtime.md`` documents it.

Design rules, all of them deliberate:

* **Offline backends only.**  The ``backend`` argument is restricted to
  ``naive`` and ``seasonal`` -- the two pure ``numpy``/``pandas`` backends
  shipped with the package.  The operational flow therefore runs on ``.[dev]``
  alone, needs no checkpoint download and imports neither ``torch`` nor
  ``timesfm``.  The model-backed backend stays reachable through
  ``trading forecast-build --backend timesfm``, which is the command that owns
  the optional extra.
* **Deterministic.**  Nothing here reads the wall clock: the same profiles file
  and the same candle file always produce the same artifact, which is what makes
  the flow reproducible in CI and in a test.
* **Cheap to import.**  This module imports :mod:`trading_platform.core.errors`
  and :mod:`trading_platform.forecast.artifact` at module level and everything
  that knows about profiles, candle files or the backend registry **inside** the
  function body, mirroring the import policy of ``trading_platform.cli``.

The matching realtime profile looks like::

    {
      "id": "btc-timesfm-paper",
      "symbol": "BTC/USDT",
      "timeframe": "1h",
      "strategy": "timesfm",
      "forecast": "data/forecast/btc-timesfm-paper-1h-seasonal.parquet"
    }

and the three-command flow around it is ``make forecast-flow``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trading_platform.core.errors import ForecastError
from trading_platform.forecast.artifact import (
    ForecastBuildConfig,
    build_forecast_artifact,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from trading_platform.config import AppConfig
    from trading_platform.forecast.artifact import ArtifactMetadata

__all__ = ["bootstrap_profile_forecast"]

#: Backends this command may use: the two fully offline ones.
#:
#: ``timesfm`` is deliberately absent.  Bootstrapping a profile must stay
#: reproducible with ``.[dev]`` alone and must never download a checkpoint, so a
#: model-backed artifact is built by ``trading forecast-build --backend timesfm``
#: -- the command that documents the extra -- and then declared by the profile.
OFFLINE_BACKENDS: tuple[str, ...] = ("naive", "seasonal")

#: Trailing window of the seasonal estimate used by a bootstrapped artifact.
DEFAULT_SEASONAL_WINDOW: int = 168

#: Largest context handed to the backend, in candles.
MAX_CONTEXT: int = 512

#: Smallest context handed to the backend, in candles.
MIN_CONTEXT: int = 32


def _resolve_backend_name(backend: str) -> str:
    """Return ``backend`` when it is an offline one, or raise :class:`ForecastError`.

    The check is a whitelist rather than a call into the registry on purpose: a
    typo must be reported as "the offline flow cannot use this name", with the
    two names it *can* use, instead of as a registry error that invites the
    operator to install a heavier extra just to bootstrap a profile.
    """
    name = str(backend).strip().lower()
    if name not in OFFLINE_BACKENDS:
        supported = ", ".join(OFFLINE_BACKENDS)
        raise ForecastError(
            f"forecast-bootstrap only supports the offline backends ({supported}), got "
            f"{backend!r}; build a model-backed artifact with "
            f"'trading forecast-build --backend timesfm' and declare it in the profile"
        )
    return name


def _candle_path(data_dir: Path, symbol: str, timeframe: str) -> Path:
    """Return the conventional candle path of ``symbol``/``timeframe``.

    The cache names every file ``<symbol>-<timeframe><suffix>`` with the pair's
    ``/`` written as ``_`` (``data/BTC_USDT-1h.csv``), which is the same
    convention :func:`trading_platform.cli.parse_data_file_stem` parses.
    """
    return Path(data_dir) / f"{symbol.replace('/', '_')}-{timeframe}.csv"


def bootstrap_profile_forecast(
    profiles_path: str | Path,
    profile_id: str | None,
    *,
    config: AppConfig,
    backend: str = "seasonal",
) -> tuple[Path, ArtifactMetadata]:
    """Build the offline artifact of one profile and write it under the data dir.

    The profile is read from ``profiles_path`` and the artifact is built from the
    candle file the profile's own declarations imply, through
    :func:`trading_platform.forecast.artifact.build_forecast_artifact` -- the one
    artifact builder of the project.  The symbol and the timeframe recorded in
    the metadata are the profile's, and they are cross-checked against the file
    name that was actually read, so a profile pointing at another instrument's
    candles is refused instead of producing a plausible-looking artifact.

    Parameters
    ----------
    profiles_path:
        JSON profiles document, read through
        :func:`trading_platform.config.loader.load_profiles`.
    profile_id:
        Id of the profile to bootstrap, or ``None`` to take the first declared
        profile.  An unknown id is refused and the message lists the known ones.
    config:
        Application configuration; ``config.data.data_dir`` locates both the
        candle file and the ``forecast/`` output directory.
    backend:
        Registry name of an **offline** backend, one of :data:`OFFLINE_BACKENDS`.
        ``timesfm`` is deliberately not accepted here: see the module docstring.

    Returns
    -------
    tuple[pathlib.Path, ArtifactMetadata]
        The written artifact path and its metadata, so a caller can print the
        coverage block without loading the artifact back.

    Raises
    ------
    ForecastError
        If the profiles file declares no such profile, if the backend is not one
        of :data:`OFFLINE_BACKENDS`, if the candle file is missing or empty, if
        the profile's declarations disagree with the candle file name, or if the
        build itself is invalid (raised unchanged by
        ``build_forecast_artifact``).
    """
    # Local imports: this module must stay cheap to import, and neither the
    # profiles loader nor the backend registry belongs to its import graph.
    from trading_platform.cli import _read_data_file, parse_data_file_stem
    from trading_platform.config.loader import load_profiles
    from trading_platform.forecast.registry import get_backend
    from trading_platform.forecast.series import resolve_seasonal_period
    from trading_platform.forecast.types import DEFAULT_QUANTILE_LEVELS

    declared = load_profiles(profiles_path)
    if profile_id is None:
        profile = declared[0]
    else:
        matches = [entry for entry in declared if entry.id == profile_id]
        if not matches:
            known = ", ".join(sorted(entry.id for entry in declared))
            raise ForecastError(
                f"profile {profile_id!r} is not declared by {profiles_path} (known ids: {known})"
            )
        profile = matches[0]

    backend_name = _resolve_backend_name(backend)

    data_dir = Path(config.data.data_dir)
    candle_path = _candle_path(data_dir, str(profile.symbol), str(profile.timeframe))
    if not candle_path.is_file():
        raise ForecastError(
            f"the candle file {candle_path} that profile {profile.id!r} implies does not exist; "
            f"download it with 'trading data download --symbol {profile.symbol} --timeframe "
            f"{profile.timeframe}' or point the profile at a symbol/timeframe whose file is there"
        )

    frame = _read_data_file(candle_path)

    # The file name is the ground truth of what was actually read: when it
    # disagrees with the profile's declarations, the artifact would be stamped
    # with a symbol or a timeframe its numbers do not describe -- exactly the
    # mismatch the realtime guard then refuses.  Refuse it here instead, where
    # the operator can still act.
    file_symbol, file_timeframe = parse_data_file_stem(candle_path)
    if file_symbol is not None and file_symbol != str(profile.symbol):
        raise ForecastError(
            f"the candle file {candle_path} holds {file_symbol!r} but profile "
            f"{profile.id!r} declares {profile.symbol!r}; rename the file or fix the profile "
            "so the artifact is not stamped with a symbol it does not describe"
        )
    if file_timeframe is not None and file_timeframe != str(profile.timeframe):
        raise ForecastError(
            f"the candle file {candle_path} holds the {file_timeframe} timeframe but profile "
            f"{profile.id!r} declares {profile.timeframe}; rename the file or fix the profile "
            "so the artifact is not stamped with a timeframe it does not describe"
        )

    context_length = max(MIN_CONTEXT, min(int(profile.warmup_candles), MAX_CONTEXT))
    build_config = ForecastBuildConfig(
        symbol=str(profile.symbol),
        timeframe=str(profile.timeframe),
        backend=backend_name,
        context_length=context_length,
        seasonal_period=resolve_seasonal_period(str(profile.timeframe)),
        seasonal_window=DEFAULT_SEASONAL_WINDOW,
    )
    backend_instance = get_backend(
        backend_name,
        timeframe=str(profile.timeframe),
        quantile_levels=DEFAULT_QUANTILE_LEVELS,
    )
    out = data_dir / "forecast" / f"{profile.id}-{profile.timeframe}-{backend_name}.parquet"
    return build_forecast_artifact(frame, build_config, out, backend=backend_instance)
