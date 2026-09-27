"""Aggregated HTTP API of the trading platform.

The package is the dashboard's only backend:

* :mod:`trading_platform.api.app` -- assembly of the FastAPI application and the
  (opt-in) engine lifecycle;
* :mod:`trading_platform.api.routes` -- every route, mounted under ``/api``;
* :mod:`trading_platform.api.schemas` -- the request bodies, plus the response
  models re-exported from :mod:`trading_platform.models`;
* :mod:`trading_platform.api.security` -- the operator token that guards every
  mutating route.

The public names are re-exported here so a caller can import either the package
or the exact submodule.
"""

from __future__ import annotations

from .app import API_PREFIX, create_app
from .routes import router
from .security import (
    INVALID_TOKEN_DETAIL,
    MISSING_TOKEN_DETAIL,
    OPERATOR_TOKEN_HEADER,
    UNCONFIGURED_TOKEN_DETAIL,
    require_operator_token,
)

__all__ = [
    "API_PREFIX",
    "INVALID_TOKEN_DETAIL",
    "MISSING_TOKEN_DETAIL",
    "OPERATOR_TOKEN_HEADER",
    "UNCONFIGURED_TOKEN_DETAIL",
    "create_app",
    "require_operator_token",
    "router",
]
