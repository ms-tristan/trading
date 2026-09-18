"""Exchange credentials, read from the environment and never written anywhere.

Delivery brief D9 forbids a credential from touching the repository: there is no
credential field in any configuration model (``extra="forbid"`` makes a profile
file that carries ``api_key`` fail loudly with a :class:`ConfigError`), so the
only supported channel is the process environment.

Two naming schemes are supported, from the most specific to the least specific:

* per profile -- ``TB_PROFILE_<UPPER_ID>_API_KEY`` / ``_API_SECRET`` /
  ``_API_PASSWORD``, where ``<UPPER_ID>`` is built by :func:`profile_env_prefix`
  (``"btc-paper"`` becomes ``TB_PROFILE_BTC_PAPER``);
* global -- ``TB_LIVE_API_KEY`` / ``TB_LIVE_API_SECRET`` /
  ``TB_LIVE_API_PASSWORD``, shared by every live profile.

The module is read-only by construction: it has no setter, no writer and no
serialiser that would ever emit a secret.  :class:`ExchangeCredentials`
redacts *all* of its textual surfaces -- ``repr()``, ``str()`` and
:meth:`ExchangeCredentials.to_dict` -- without needing a flag, so a credential
object can be logged, persisted or shipped to the dashboard by mistake without
leaking anything.  :func:`redact` and :func:`known_secrets` are the two helpers
the structured-logging layer uses to scrub free-form messages.

Security note
-------------
The environment of the process remains the real secret store: this module only
*reads* it.  Nothing here prevents ``os.environ`` from leaking through a
traceback or a ``/proc`` read by a sufficiently privileged observer -- the claim
is narrower and exactly testable: no secret value ever appears in a
:class:`ExchangeCredentials` rendering, in a payload this package produces, or
in the bytes this package writes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from trading_platform.core.errors import ProfileError

__all__ = [
    "ENV_LIVE_API_KEY",
    "ENV_LIVE_API_PASSWORD",
    "ENV_LIVE_API_SECRET",
    "PROFILE_ENV_PREFIX",
    "REDACTED",
    "ExchangeCredentials",
    "credentials_from_env",
    "known_secrets",
    "profile_env_prefix",
    "redact",
]

#: Environment variable holding the API key of every live profile.
ENV_LIVE_API_KEY = "TB_LIVE_API_KEY"
#: Environment variable holding the API secret of every live profile.
ENV_LIVE_API_SECRET = "TB_LIVE_API_SECRET"
#: Environment variable holding the API password (some venues need a passphrase).
ENV_LIVE_API_PASSWORD = "TB_LIVE_API_PASSWORD"

#: Prefix of the per-profile environment variables, before the upper-cased id.
PROFILE_ENV_PREFIX = "TB_PROFILE_"

#: Suffix of the per-profile API key variable.
SUFFIX_API_KEY = "_API_KEY"
#: Suffix of the per-profile API secret variable.
SUFFIX_API_SECRET = "_API_SECRET"
#: Suffix of the per-profile API password variable.
SUFFIX_API_PASSWORD = "_API_PASSWORD"

#: The only placeholder a redacted value is ever replaced with.
REDACTED = "***"

#: Environment-variable name -> the field of :class:`ExchangeCredentials` it fills.
_SUFFIXES: tuple[str, ...] = (SUFFIX_API_KEY, SUFFIX_API_SECRET, SUFFIX_API_PASSWORD)

#: Every character an identifier may keep in an environment-variable name.
_ENV_UNSAFE = re.compile(r"[^A-Z0-9]")


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return ``environ`` when given, ``os.environ`` otherwise."""
    return os.environ if environ is None else environ


def _mask(value: str) -> str:
    """Return :data:`REDACTED` for a non-empty value, ``""`` for an empty one.

    The empty case matters: rendering ``password=***`` when no password was ever
    configured would suggest a secret exists where none does.
    """
    return REDACTED if value else ""


def profile_env_prefix(profile_id: str) -> str:
    """Return the environment-variable prefix of ``profile_id``.

    Every character that is neither a letter nor a digit is replaced by an
    underscore, after upper-casing, so ``"btc-paper"``, ``"btc.paper"`` and
    ``"BTC/USDT"`` all map to a syntactically valid variable name.

    Examples
    --------
    >>> profile_env_prefix("btc-paper")
    'TB_PROFILE_BTC_PAPER'
    >>> profile_env_prefix("btc.usdt-live")
    'TB_PROFILE_BTC_USDT_LIVE'
    """
    return PROFILE_ENV_PREFIX + _ENV_UNSAFE.sub("_", str(profile_id).upper())


@dataclass(frozen=True, repr=False)
class ExchangeCredentials:
    """The credentials of one venue connection, always rendered redacted.

    Parameters
    ----------
    api_key:
        Public identifier of the API key.
    api_secret:
        Private secret.  Never rendered by this class.
    password:
        Optional passphrase (some venues require one).
    exchange:
        Venue name, e.g. ``"binance"``.
    profile_id:
        Owning profile, empty for the global credential set.
    source:
        Where the credentials came from: ``"profile-env"``, ``"live-env"`` or
        ``""`` when they were built by hand.
    """

    api_key: str
    api_secret: str
    password: str = ""
    exchange: str = "binance"
    profile_id: str = ""
    source: str = ""

    @property
    def configured(self) -> bool:
        """Whether both a key and a secret are present."""
        return bool(self.api_key and self.api_secret)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping whose secrets are redacted.

        There is deliberately **no** parameter to ask for the real values: the
        only representation this method can produce is the safe one.
        """
        return {
            "api_key": _mask(self.api_key),
            "api_secret": _mask(self.api_secret),
            "password": _mask(self.password),
            "exchange": str(self.exchange),
            "profile_id": str(self.profile_id),
            "configured": self.configured,
            "source": str(self.source),
        }

    def __repr__(self) -> str:
        """Return a debug representation that never contains a secret."""
        return (
            "ExchangeCredentials("
            f"api_key={_mask(self.api_key)!r}, "
            f"api_secret={_mask(self.api_secret)!r}, "
            f"password={_mask(self.password)!r}, "
            f"exchange={self.exchange!r}, "
            f"profile_id={self.profile_id!r}, "
            f"source={self.source!r})"
        )

    def __str__(self) -> str:
        """Return :meth:`__repr__` (``str()`` must be as safe as ``repr()``)."""
        return repr(self)


def credentials_from_env(
    profile_id: str = "",
    *,
    exchange: str = "binance",
    environ: Mapping[str, str] | None = None,
) -> ExchangeCredentials | None:
    """Resolve the credentials of ``profile_id`` from the environment.

    The per-profile variables win over the global ones: a profile that declares
    its own ``TB_PROFILE_<ID>_API_KEY`` never falls back to ``TB_LIVE_API_KEY``,
    even when the global pair is incomplete.

    Parameters
    ----------
    profile_id:
        Profile whose per-profile variables are tried first; empty means
        "global only".
    exchange:
        Venue name stored on the returned object (the caller knows the venue).
    environ:
        Mapping to read instead of ``os.environ`` (tests inject one, so nothing
        has to be patched globally).

    Returns
    -------
    ExchangeCredentials | None
        ``None`` when no API key is present in either naming scheme.

    Raises
    ------
    ProfileError
        When a key is present without its secret under the same scheme.  Failing
        loudly beats connecting with half a credential.
    """
    env = _environment(environ)
    if profile_id:
        prefix = profile_env_prefix(profile_id)
        api_key = env.get(prefix + SUFFIX_API_KEY, "")
        if api_key:
            api_secret = env.get(prefix + SUFFIX_API_SECRET, "")
            if not api_secret:
                raise ProfileError(
                    f"profile {profile_id!r} sets {prefix}{SUFFIX_API_KEY} "
                    f"but no {prefix}{SUFFIX_API_SECRET}"
                )
            return ExchangeCredentials(
                api_key=api_key,
                api_secret=api_secret,
                password=env.get(prefix + SUFFIX_API_PASSWORD, ""),
                exchange=exchange,
                profile_id=profile_id,
                source="profile-env",
            )
    api_key = env.get(ENV_LIVE_API_KEY, "")
    if not api_key:
        return None
    api_secret = env.get(ENV_LIVE_API_SECRET, "")
    if not api_secret:
        raise ProfileError(f"{ENV_LIVE_API_KEY} is set but no {ENV_LIVE_API_SECRET}")
    return ExchangeCredentials(
        api_key=api_key,
        api_secret=api_secret,
        password=env.get(ENV_LIVE_API_PASSWORD, ""),
        exchange=exchange,
        profile_id=profile_id,
        source="live-env",
    )


def known_secrets(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return every non-empty secret value currently visible in the environment.

    The result feeds :func:`redact` and the redaction filter of the structured
    logging layer, which turn it into a set of literal patterns to scrub.  Empty
    values are skipped (replacing ``""`` would corrupt every message), and the
    result is sorted and de-duplicated so a log line is stable across runs.
    """
    env = _environment(environ)
    found: set[str] = set()
    for name, value in env.items():
        if not value:
            continue
        if name in (ENV_LIVE_API_KEY, ENV_LIVE_API_SECRET, ENV_LIVE_API_PASSWORD) or (
            name.startswith(PROFILE_ENV_PREFIX) and name.endswith(_SUFFIXES)
        ):
            found.add(str(value))
    return tuple(sorted(found))


def redact(value: str | None, *, secrets: Iterable[str] = ()) -> str:
    """Return ``value`` with every known secret replaced by :data:`REDACTED`.

    Parameters
    ----------
    value:
        Text to scrub; ``None`` renders as the empty string so a caller can pass
        an optional field straight through.
    secrets:
        Literal secret values, typically :func:`known_secrets` output.  Empty
        strings are ignored -- a pattern that matches nothing cannot leak, but it
        would replace every position and destroy the message.
    """
    if value is None:
        return ""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), REDACTED)
    return text
