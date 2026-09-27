"""Multi-profile crypto trading platform supervised by Freqtrade.

The package is organised as follows:

* :mod:`trading_platform.paths` -- filesystem layout resolution;
* :mod:`trading_platform.config` -- platform settings and the live-trading gate;
* :mod:`trading_platform.models` -- the single source of truth for every JSON shape;
* :mod:`trading_platform.metrics` -- aggregation of per-profile data into views;
* :mod:`trading_platform.logging_setup` -- process-wide logging configuration.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
