"""Reporting layer: report model, Markdown/JSON rendering and file writer.

Public surface (frozen contract)::

    from trading_platform.reporting import (
        Report, ReportBuilder, ReportSection, build_report,
        render_markdown, render_summary_table, write_report, read_report,
    )

This layer depends on :mod:`trading_platform.core` and
:mod:`trading_platform.metrics` only; the validation payloads it renders are
passed as opaque mappings, so it never imports the validation or strategy layers.
"""

from __future__ import annotations

from trading_platform.core.errors import ReportingError
from trading_platform.reporting.builder import (
    Report,
    ReportBuilder,
    ReportSection,
    build_report,
)
from trading_platform.reporting.markdown import render_markdown, render_summary_table
from trading_platform.reporting.writer import SUPPORTED_FORMATS, read_report, write_report

__all__ = [
    "SUPPORTED_FORMATS",
    "Report",
    "ReportBuilder",
    "ReportSection",
    "ReportingError",
    "build_report",
    "read_report",
    "render_markdown",
    "render_summary_table",
    "write_report",
]
