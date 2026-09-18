"""Modular, robust and scalable backtesting skeleton for trading strategies.

The package is organised in layers with a strict dependency direction::

    core   <-  config  <-  data  <-  strategy  <-  validation  <-  metrics/reporting  <-  cli

Only :mod:`trading_platform.core` defines the cross-package domain models.
"""

from __future__ import annotations

__version__: str = "0.1.0"

__all__ = ["__version__"]
