"""Writing/reading reports to and from disk.

Two formats are supported: ``markdown`` (``{basename}.md``) and ``json``
(``{basename}.json``).  Formats are validated *before* anything is written, so an
unsupported format never leaves a half-written report behind.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trading_backtest.core.errors import ReportingError

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from trading_backtest.reporting.builder import Report

__all__ = [
    "SUPPORTED_FORMATS",
    "read_report",
    "write_report",
]

#: Format name -> file suffix.
SUPPORTED_FORMATS: dict[str, str] = {"markdown": ".md", "json": ".json"}


def _validate_formats(formats: Sequence[str]) -> list[str]:
    """Return the requested formats as a list, raising before any write."""
    requested = [str(fmt) for fmt in formats]
    for fmt in requested:
        if fmt not in SUPPORTED_FORMATS:
            raise ReportingError(f"unsupported report format: {fmt!r}")
    if not requested:
        raise ReportingError("no report format requested: refusing to create the output directory")
    return requested


def write_report(
    report: Report,
    output_dir: Path,
    *,
    formats: Sequence[str] = ("markdown", "json"),
    basename: str = "report",
) -> list[Path]:
    """Write ``report`` into ``output_dir`` and return the written paths.

    Parameters
    ----------
    report:
        The report to persist.
    output_dir:
        Target directory; created (with parents) when it does not exist.  It is
        only created once the requested formats have been validated.
    formats:
        Subset/sequence of ``("markdown", "json")``, written in the given order.
    basename:
        File name stem used for both formats.

    Returns
    -------
    list[pathlib.Path]
        Paths of the written files, in the same order as ``formats``.

    Raises
    ------
    ReportingError
        If a format is unsupported or if ``formats`` is empty.  Nothing is
        written and no directory is created in that case.
    """
    requested = _validate_formats(formats)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fmt in requested:
        path = directory / f"{basename}{SUPPORTED_FORMATS[fmt]}"
        content = report.to_markdown() if fmt == "markdown" else report.to_json()
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def read_report(path: str | Path) -> dict[str, Any]:
    """Read back a JSON report written by :func:`write_report`.

    Raises
    ------
    ReportingError
        If the file is missing, unreadable, or does not contain a JSON object.
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ReportingError(f"cannot read report {target}: {error}") from error
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise ReportingError(f"invalid report JSON in {target}: {error}") from error
    if not isinstance(payload, dict):
        raise ReportingError(f"report {target} does not contain a JSON object")
    return payload
