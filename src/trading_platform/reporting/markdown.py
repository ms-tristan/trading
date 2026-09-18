"""Deterministic Markdown rendering of a :class:`~trading_platform.reporting.builder.Report`.

Rendering rules (frozen contract):

* ``# {title}`` then ``_Generated at {generated_at}_`` then a ``## Summary``
  table (metric name order is alphabetical, floats use ``%.4f``);
* metadata is rendered as a bullet list when present;
* every :class:`~trading_platform.reporting.builder.ReportSection` becomes a
  ``##``/``###`` heading followed by bullets (mapping body) or a table
  (sequence-of-mappings body) whose columns are the union of the row keys, sorted;
* an empty body renders as ``_(empty)_``;
* the output has no trailing whitespace and ends with exactly one newline, and is
  byte-identical between two calls with the same report.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Integral, Real
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from trading_platform.reporting.builder import Report, ReportSection

__all__ = [
    "render_markdown",
    "render_summary_table",
]

#: Section/column headings used by the two fixed tables.
SUMMARY_HEADERS = ("Metric", "Value")

#: Summary keys rendered as integers (counts) instead of ``%.4f``.
COUNT_KEYS = frozenset(
    {
        "count",
        "n_folds",
        "n_iterations",
        "n_periods",
        "n_simulations",
        "n_splits",
        "n_trades",
        "n_windows",
        "total_trades",
        "trades",
    }
)

EMPTY_PLACEHOLDER = "_(empty)_"


def _format_scalar(key: str, value: Any) -> str:
    """Format one cell/one bullet value deterministically."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Integral):
        return str(int(value))
    if isinstance(value, Real):
        number = float(value)
        if math.isnan(number):
            return "nan"
        if math.isinf(number):
            return "inf" if number > 0.0 else "-inf"
        if key in COUNT_KEYS:
            return str(int(number))
        return f"{number:.4f}"
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return ", ".join(
            f"{nested_key}={_format_scalar(str(nested_key), value[nested_key])}"
            for nested_key in sorted(value, key=str)
        )
    if isinstance(value, (set, frozenset)):
        return ", ".join(sorted(_format_scalar(key, item) for item in value))
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_scalar(key, item) for item in value)
    return str(value)


def _bullet_lines(mapping: Mapping[str, Any], *, indent: int = 0) -> list[str]:
    """Render ``mapping`` as ``- key: value`` bullets, recursing one level."""
    pad = "  " * indent
    lines: list[str] = []
    for key in sorted(mapping, key=str):
        value = mapping[key]
        if isinstance(value, Mapping) and indent == 0 and value:
            lines.append(f"{pad}- {key}:")
            lines.extend(_bullet_lines(value, indent=indent + 1))
        else:
            lines.append(f"{pad}- {key}: {_format_scalar(str(key), value)}".rstrip())
    return lines


def _render_rows_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render rows as a Markdown table whose columns are the sorted key union."""
    columns = sorted({str(key) for row in rows for key in row}, key=str)
    if not columns:
        return EMPTY_PLACEHOLDER
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        cells = [_format_scalar(column, row.get(column)) for column in columns]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_summary_table(mapping: Mapping[str, float]) -> str:
    """Render a ``| Metric | Value |`` table, sorted by metric name."""
    lines = [
        f"| {SUMMARY_HEADERS[0]} | {SUMMARY_HEADERS[1]} |",
        "| --- | --- |",
    ]
    for name in sorted(mapping, key=str):
        lines.append(f"| {name} | {_format_scalar(str(name), mapping[name])} |")
    return "\n".join(lines)


def _render_body(section: ReportSection) -> str:
    """Render the body of ``section`` as bullets, a table or the empty placeholder."""
    body = section.body
    if isinstance(body, Mapping):
        return "\n".join(_bullet_lines(body)) if body else EMPTY_PLACEHOLDER
    if isinstance(body, Sequence) and body:
        return _render_rows_table(body)
    return EMPTY_PLACEHOLDER


def _render_section(section: ReportSection) -> str:
    """Render one section: heading, blank line, body."""
    level = max(1, min(int(section.level), 6))
    heading = "#" * level
    return "\n".join([f"{heading} {section.title}", "", _render_body(section)])


def render_markdown(report: Report) -> str:
    """Render ``report`` as deterministic Markdown ending with a single newline."""
    blocks = [
        f"# {report.title}",
        f"_Generated at {report.generated_at.isoformat()}_",
        "\n".join(["## Summary", "", render_summary_table(report.summary)]),
    ]
    if report.metadata:
        blocks.append("\n".join(["## Metadata", "", *_bullet_lines(report.metadata)]))
    blocks.extend(_render_section(section) for section in report.sections)
    return "\n\n".join(blocks).rstrip() + "\n"
