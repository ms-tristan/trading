"""Operator-token authentication of the mutating API routes.

The platform has exactly one credential of its own: the operator token of
``TB_OPERATOR_TOKEN``. It is compared with :func:`secrets.compare_digest`, so a
wrong token cannot be discovered byte by byte through response timing, and it is
never echoed back, logged or stored -- the environment is the only place it
lives.

Three refusals, three distinct meanings:

* the header is absent -> ``401``: the caller did not authenticate at all;
* the header is present but wrong -> ``403``: the caller tried and failed;
* ``TB_OPERATOR_TOKEN`` is unset or empty -> ``403``: the *server* is not
  configured, so nobody can be authorised. This check comes first on purpose: a
  platform without a token must refuse every mutation, including one that sends
  no header at all, instead of answering ``401`` as if a token existed.

Reading routes (health, profiles, account, strategies, events, settings) are
deliberately public: the dashboard is served same-origin through its ``/api/*``
rewrite and the deployment publishes the API on ``127.0.0.1`` only. CORS stays
disabled (`allow_origins` is never configured), so a foreign page cannot read
those routes from a browser.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from ..config import operator_token

__all__ = [
    "INVALID_TOKEN_DETAIL",
    "MISSING_TOKEN_DETAIL",
    "OPERATOR_TOKEN_HEADER",
    "UNCONFIGURED_TOKEN_DETAIL",
    "require_operator_token",
]

#: Header carrying the operator token of every mutating call.
OPERATOR_TOKEN_HEADER = "X-Operator-Token"

#: Detail of the ``401`` answered when the header is absent.
MISSING_TOKEN_DETAIL = "missing operator token"

#: Detail of the ``403`` answered when the header does not match the token.
INVALID_TOKEN_DETAIL = "invalid operator token"

#: Detail of the ``403`` answered when ``TB_OPERATOR_TOKEN`` is unset or empty.
UNCONFIGURED_TOKEN_DETAIL = "operator token is not configured"


def require_operator_token(
    x_operator_token: Annotated[str | None, Header(alias=OPERATOR_TOKEN_HEADER)] = None,
) -> None:
    """Authorise one mutating request, or raise the matching :class:`HTTPException`.

    Mount it on a route with ``dependencies=[Depends(require_operator_token)]``:
    the dependency runs before the body is handled, so an unauthorised request
    never reaches the state store.

    The comparison is done on the UTF-8 bytes of both values, which keeps
    :func:`secrets.compare_digest` usable whatever the token contains (it only
    accepts ASCII ``str`` operands) and keeps the timing behaviour constant.
    """
    expected = operator_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=UNCONFIGURED_TOKEN_DETAIL,
        )
    if x_operator_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=MISSING_TOKEN_DETAIL,
        )
    if not secrets.compare_digest(x_operator_token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=INVALID_TOKEN_DETAIL,
        )
