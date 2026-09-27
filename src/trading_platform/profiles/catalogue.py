"""Discovery of the strategy catalogue and of the declarative profile catalogue.

Three sources drive what the platform offers:

* ``config/strategies.json`` -- the metadata of every shipped strategy (title,
  category, indicators, timeframes, reference, risk notes);
* ``user_data/strategies/*.py`` -- the strategy files themselves;
* ``config/profiles.json`` -- the declarative profiles the engine is asked to run.

The strategy rule is deliberately permissive. A strategy file that has **no**
metadata entry is still part of the catalogue: its id is derived from the class
name (``MyThingStrategy`` -> ``my-thing``), its title from the class name itself
and its description stays empty, so dropping a file into ``user_data/strategies``
is enough to make it usable. Symmetrically, a metadata entry whose file is
missing is still returned: a half-configured checkout must never make the
dashboard fail, and the operator may well be about to add the file.

The profile loader is stricter, because a profile is what the engine actually
runs: an entry that does not validate as a :class:`~trading_platform.models.ProfileConfig`
raises :class:`ValueError` naming the offending profile id instead of being
silently dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..models import ProfileConfig, StrategyMeta
from ..paths import CONFIG_DIR, STRATEGIES_DIR

__all__ = [
    "PROFILES_DOCUMENT",
    "STRATEGIES_DOCUMENT",
    "STRATEGY_CATEGORY_DEFAULT",
    "StrategyCatalogue",
    "discover_strategy_files",
    "load_profile_catalogue",
    "load_strategy_catalogue",
]

#: Metadata document of the strategy catalogue.
STRATEGIES_DOCUMENT = "strategies.json"

#: Declarative profile catalogue applied to a running engine.
PROFILES_DOCUMENT = "profiles.json"

#: Category given to a strategy that has no metadata entry.
STRATEGY_CATEGORY_DEFAULT = "baseline"

#: Class-name suffix Freqtrade's resolver relies on; it is stripped to build the id.
_STRATEGY_SUFFIX = "Strategy"

_LOWER_TO_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_TO_WORD = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_SPACE_RUN = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Name derivation
# ---------------------------------------------------------------------------
def _split_words(class_name: str) -> list[str]:
    """Split a CamelCase class name into its words (``MACDStrategy`` -> MACD, Strategy)."""
    spaced = _LOWER_TO_UPPER.sub(" ", class_name.strip())
    spaced = _ACRONYM_TO_WORD.sub(" ", spaced)
    return [word for word in _SPACE_RUN.split(spaced) if word]


def _strategy_id_from_class_name(class_name: str) -> str:
    """Return the catalogue id of a class name minus its ``Strategy`` suffix.

    ``MyThingStrategy`` -> ``my-thing``, ``RsiReversionStrategy`` ->
    ``rsi-reversion``, ``MACDStrategy`` -> ``macd``.
    """
    name = class_name.strip()
    if name.endswith(_STRATEGY_SUFFIX) and len(name) > len(_STRATEGY_SUFFIX):
        name = name[: -len(_STRATEGY_SUFFIX)]
    return _NON_WORD.sub("-", " ".join(_split_words(name)).lower()).strip("-")


def _strategy_title_from_class_name(class_name: str) -> str:
    """Return the human title derived from a class name.

    ``MyThingStrategy`` becomes ``My Thing Strategy``.
    """
    return " ".join(_split_words(class_name))


# ---------------------------------------------------------------------------
# JSON documents
# ---------------------------------------------------------------------------
def _read_document(path: Path) -> dict[str, Any] | None:
    """Return the JSON object stored at ``path``; ``None`` when unusable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


def _document_entries(document: dict[str, Any] | None, key: str) -> list[Any]:
    """Return the list stored under ``key``, or an empty list when absent."""
    if document is None:
        return []
    entries = document.get(key)
    return list(entries) if isinstance(entries, list) else []


def _validation_message(exc: ValidationError) -> str:
    """Render a pydantic error as one compact, credential-free line."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<entry>'}: {error['msg']}"
        for error in exc.errors()
    )


# ---------------------------------------------------------------------------
# Strategy catalogue
# ---------------------------------------------------------------------------
class StrategyCatalogue:
    """The strategies the platform knows about, indexed by id and by class name.

    The container keeps the insertion order, which is the order of
    ``config/strategies.json`` followed by the discovered files sorted by name.
    """

    def __init__(self, strategies: Sequence[StrategyMeta]) -> None:
        self._by_id: dict[str, StrategyMeta] = {}
        self._by_class_name: dict[str, StrategyMeta] = {}
        for meta in strategies:
            self._by_id[meta.id] = meta
            if meta.class_name:
                self._by_class_name.setdefault(meta.class_name, meta)

    def get(self, strategy_id: str) -> StrategyMeta | None:
        """Return the strategy registered under ``strategy_id``, or ``None``."""
        return self._by_id.get(strategy_id)

    def by_class_name(self, class_name: str) -> StrategyMeta | None:
        """Return the strategy whose Freqtrade class is ``class_name``, or ``None``."""
        return self._by_class_name.get(class_name)

    def all(self) -> list[StrategyMeta]:
        """Return every strategy, in insertion order."""
        return list(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[StrategyMeta]:
        return iter(self._by_id.values())

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._by_id


def discover_strategy_files(strategies_dir: Path | None = None) -> list[str]:
    """Return the sorted ``*.py`` stems of ``user_data/strategies``.

    Files starting with an underscore (``__init__.py``, ``_helpers.py``) are not
    strategies and are skipped, and a missing directory simply discovers nothing.
    """
    directory = STRATEGIES_DIR if strategies_dir is None else Path(strategies_dir)
    try:
        candidates = list(directory.glob("*.py"))
    except OSError:  # pragma: no cover - unreadable directory
        return []
    return sorted(
        path.stem for path in candidates if path.is_file() and not path.name.startswith("_")
    )


def load_strategy_catalogue(
    config_path: Path | None = None,
    strategies_dir: Path | None = None,
) -> StrategyCatalogue:
    """Build the strategy catalogue from ``config/strategies.json`` and the files on disk.

    Metadata entries are read first, in document order. Every discovered strategy
    file that no entry already describes is then appended with a derived id, a
    derived title and the ``baseline`` category. A strategy file is considered
    already described when an entry carries its derived id, its class name or its
    file name, which is what keeps a half-declared strategy from showing up twice.
    """
    document_path = CONFIG_DIR / STRATEGIES_DOCUMENT if config_path is None else Path(config_path)
    document = _read_document(document_path)

    entries: list[StrategyMeta] = []
    covered: set[str] = set()
    for raw_entry in _document_entries(document, "strategies"):
        if not isinstance(raw_entry, dict):
            continue
        try:
            meta = StrategyMeta.model_validate(raw_entry)
        except ValidationError:
            # A malformed metadata entry is skipped rather than fatal: the file
            # on disk is still discovered below, so the strategy stays usable.
            continue
        entries.append(meta)
        covered.add(meta.id)
        if meta.class_name:
            covered.add(_strategy_id_from_class_name(meta.class_name))
        if meta.file:
            covered.add(_strategy_id_from_class_name(Path(meta.file).stem))

    for stem in discover_strategy_files(strategies_dir):
        derived_id = _strategy_id_from_class_name(stem)
        if not derived_id or derived_id in covered:
            continue
        entries.append(
            StrategyMeta(
                id=derived_id,
                class_name=stem,
                file=f"{stem}.py",
                title=_strategy_title_from_class_name(stem),
                category=STRATEGY_CATEGORY_DEFAULT,
            )
        )
    return StrategyCatalogue(entries)


# ---------------------------------------------------------------------------
# Profile catalogue
# ---------------------------------------------------------------------------
def load_profile_catalogue(config_path: Path | None = None) -> list[ProfileConfig]:
    """Read the declarative profile catalogue, in document order.

    A missing or unreadable document yields an empty catalogue; an entry that
    does not validate raises :class:`ValueError` naming the offending profile id,
    so a broken catalogue is reported at boot instead of halfway through a
    deployment.
    """
    document_path = CONFIG_DIR / PROFILES_DOCUMENT if config_path is None else Path(config_path)
    document = _read_document(document_path)

    profiles: list[ProfileConfig] = []
    for index, raw_entry in enumerate(_document_entries(document, "profiles")):
        if not isinstance(raw_entry, dict):
            raise ValueError(
                f"invalid profile catalogue entry at index {index}: expected a JSON object"
            )
        try:
            profiles.append(ProfileConfig.model_validate(raw_entry))
        except ValidationError as exc:
            profile_id = raw_entry.get("id") or f"<index {index}>"
            raise ValueError(
                f"invalid profile catalogue entry {profile_id!r}: {_validation_message(exc)}"
            ) from exc
    return profiles
