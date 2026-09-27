"""Trading engine: one Freqtrade child process per profile plus its REST client.

The package groups the pieces the realtime supervisor drives:

* :mod:`trading_platform.engine.config_builder` -- the generated Freqtrade
  configuration, the per-profile runtime layout and the ``freqtrade trade``
  command line;
* :mod:`trading_platform.engine.client` -- the async REST client of the API
  server of a single profile;
* :mod:`trading_platform.engine.supervisor` and
  :mod:`trading_platform.engine.poller` -- process supervision and metric
  polling, which build on the two modules above.

The public names are re-exported here so that a caller can import either the
package or the exact submodule.
"""

from __future__ import annotations

from .client import FreqtradeClient, FreqtradeClientError
from .config_builder import (
    DEFAULT_THROTTLE_SECONDS,
    GENERATED_CONFIG_MODE,
    TIMEFRAME_THROTTLE_SECONDS,
    allocate_api_port,
    build_freqtrade_argv,
    build_freqtrade_config,
    profile_config_path,
    profile_data_dir,
    profile_db_url,
    profile_log_path,
    profile_runtime_dir,
    resolve_freqtrade_binary,
    throttle_for_timeframe,
    write_freqtrade_config,
)

__all__ = [
    "DEFAULT_THROTTLE_SECONDS",
    "GENERATED_CONFIG_MODE",
    "TIMEFRAME_THROTTLE_SECONDS",
    "FreqtradeClient",
    "FreqtradeClientError",
    "allocate_api_port",
    "build_freqtrade_argv",
    "build_freqtrade_config",
    "profile_config_path",
    "profile_data_dir",
    "profile_db_url",
    "profile_log_path",
    "profile_runtime_dir",
    "resolve_freqtrade_binary",
    "throttle_for_timeframe",
    "write_freqtrade_config",
]
