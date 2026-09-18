"""Documentation contract tests of the realtime delivery (work package wp10).

Pure file-content assertions: no network, no import of the heavy layers.  The
documentation *is* part of the delivered surface here (the delivery brief makes
the French architecture update part of the same change), so it is checked
mechanically: the two new layers, the whole ``RealtimeError`` branch, the four
protocol seams, the web contract and -- most importantly -- the honesty section
that says what is *not* proven.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = REPO_ROOT / "docs" / "architecture.md"
USAGE = REPO_ROOT / "docs" / "usage.md"
REALTIME = REPO_ROOT / "docs" / "realtime.md"
README = REPO_ROOT / "README.md"
MAKEFILE = REPO_ROOT / "Makefile"
EXAMPLE_PROFILES = REPO_ROOT / "config" / "profiles.example.json"

#: Every exception of the realtime branch (``core/errors.py``).
REALTIME_ERRORS = (
    "RealtimeError",
    "ProfileError",
    "MarketStreamError",
    "StateStoreError",
    "BrokerError",
    "BrokerUnavailableError",
    "OrderRejectedError",
    "GatewayError",
    "LiveTradingForbiddenError",
    "RiskLimitExceededError",
    "KillSwitchActiveError",
    "MonitoringError",
)

#: The injectable seams the architecture must spell out.
REALTIME_PROTOCOLS = ("Clock", "MarketStream", "Broker", "StateStore", "SnapshotProvider")

#: Section titles ``docs/realtime.md`` must carry.
#:
#: ``docs/realtime.md`` is written in English: it was authored by the real-time
#: delivery, and the pages that predate it (architecture, usage, methodology) stay
#: French, which is why the language test below treats the two groups differently.
REALTIME_SECTIONS = (
    "## 1. What is a profile?",
    "## 2. One execution path for paper and live",
    "## 3. The safety model",
    "## 4. Persistence, restart and reconciliation",
    "## 5. Web API reference",
    "## 6. The three commands",
    "## 7. What is NOT proven",
)

#: Routes the web-API table must enumerate.
REALTIME_ROUTES = (
    "GET /",
    "GET /static/{asset}",
    "GET /api/health",
    "GET /api/profiles",
    "GET /api/profiles/{id}",
    "GET /api/profiles/{id}/equity",
    "GET /api/profiles/{id}/trades",
    "GET /api/profiles/{id}/orders",
    "GET /api/profiles/{id}/positions",
    "GET /api/profiles/{id}/metrics",
    "POST /api/kill-switch",
)

_MD_LINK = re.compile(r"\]\(([^)\s]+)\)")

#: Substrings that must never appear in the shipped example profiles file.
FORBIDDEN_PROFILE_KEYS = ("api_key", "api_secret", "password", "secret", "token")


def read(path: Path) -> str:
    """Return the UTF-8 content of ``path``."""
    return path.read_text(encoding="utf-8")


def non_empty_lines(text: str) -> list[str]:
    """Return the stripped non-empty lines of ``text``."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def markdown_link_targets(text: str) -> list[str]:
    """Return every relative ``*.md`` link target found in ``text``."""
    targets: list[str] = []
    for raw in _MD_LINK.findall(text):
        target = raw.split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.endswith(".md"):
            targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# 7. the architecture document owns the two new layers
# ---------------------------------------------------------------------------


def test_architecture_mentions_both_new_layers() -> None:
    text = read(ARCHITECTURE)

    assert "trading_platform.realtime" in text
    assert "trading_platform.web" in text


def test_architecture_layer_table_has_the_three_new_rows() -> None:
    text = read(ARCHITECTURE)

    assert "| 6 | `trading_platform.realtime` |" in text
    assert "| 7 | `trading_platform.web` |" in text
    assert "| 8 | `trading_platform.cli` |" in text
    # the move is documented, not silently applied
    assert "déménagé de la couche 6 à la couche 8" in text
    # ... and the diagram carries the two new layers as well
    assert "│               realtime                   │" in text
    assert "│        web         │" in text


def test_architecture_documents_the_moved_cli_import_policy() -> None:
    text = read(ARCHITECTURE)

    assert "sys.modules" in text
    assert "descendant" in text
    # the Freqtrade section keeps saying what did *not* move
    assert "l'adaptateur Freqtrade ne crée" in text
    assert "freqtrade_adapter" in text


def test_architecture_documents_every_realtime_error() -> None:
    text = read(ARCHITECTURE)

    missing = [name for name in REALTIME_ERRORS if name not in text]
    assert not missing, f"architecture.md does not document: {missing}"
    assert "└── RealtimeError" in text
    assert "├── BrokerUnavailableError" in text


def test_architecture_documents_the_four_seams_and_the_snapshot_provider() -> None:
    text = read(ARCHITECTURE)

    for name in REALTIME_PROTOCOLS:
        assert f"class {name}(Protocol)" in text, f"architecture.md does not freeze {name}"
    for member in ("next_candle", "submit", "reconcile", "append_equity", "last_processed_candle"):
        assert member in text, f"architecture.md does not document {member}"


def test_architecture_documents_the_single_execution_path() -> None:
    text = read(ARCHITECTURE)

    assert "un seul chemin d'exécution" in text
    assert "ExecutionGateway" in text
    assert "si paper / si live" in text


def test_architecture_documents_the_paper_live_safety_model() -> None:
    text = read(ARCHITECTURE)

    assert "I_UNDERSTAND_THE_RISK" in text
    assert "LiveTradingGate" in text
    assert "kill switch" in text.lower()


def test_architecture_documents_the_honesty_limits() -> None:
    """The limits are the deliverable: polling, single writer, no live proof."""
    text = read(ARCHITECTURE)

    assert "WebSocket" in text, "architecture.md must state that there is no WebSocket/ASGI push"
    assert "ASGI" in text
    assert "sondage" in text or "polling" in text
    assert "PAS prouvé" in text or "non prouvé" in text
    assert "single-writer" in text or "un seul écrivain" in text
    assert "carnet d'ordres" in text, "paper fills must be described as simulated"
    assert "PAS exercé contre un vrai" in text
    assert "une frontière de timeframe après le signal" in text


def test_architecture_keeps_the_execution_equivalence_section() -> None:
    text = read(ARCHITECTURE)

    # the realtime decision point is stated with the same vocabulary as §4.9.4
    assert "clôture de `t`" in text
    assert "ouverture de `t+1`" in text
    assert "prix de référence" in text


def test_architecture_documents_the_three_realtime_commands() -> None:
    text = read(ARCHITECTURE)

    for command in ("realtime run", "realtime serve", "realtime check"):
        assert command in text, f"architecture.md does not document `{command}`"


def test_architecture_documents_the_new_tree_entries() -> None:
    text = read(ARCHITECTURE)

    for comment in (
        "# exports publics de la couche 6",
        "# RealtimeOrchestrator : N profils",
        "# read model : ProfileReport",
        "# ThreadingHTTPServer, create_server",
        "# tableau de bord hors ligne",
        "# temps réel multi-profils : profils",
        "# profils temps réel (profiles + realtime + monitoring)",
    ):
        assert comment in text, f"the §2 tree does not list {comment!r}"


# ---------------------------------------------------------------------------
# 8. docs/realtime.md exists, is substantial and is reachable
# ---------------------------------------------------------------------------


def test_realtime_page_exists_and_is_substantial() -> None:
    assert REALTIME.is_file(), "docs/realtime.md is missing"
    lines = non_empty_lines(read(REALTIME))
    assert len(lines) >= 40, f"docs/realtime.md only has {len(lines)} non-empty lines"
    assert len("\n".join(lines)) > 500


def test_realtime_page_has_the_documented_sections() -> None:
    text = read(REALTIME)

    for title in REALTIME_SECTIONS:
        assert title in text, f"docs/realtime.md does not carry the section {title!r}"


def test_realtime_page_documents_every_route() -> None:
    text = read(REALTIME)

    for route in REALTIME_ROUTES:
        assert route in text, f"docs/realtime.md does not document {route}"
    assert "|" in text, "the routes must be rendered as a table"
    assert "2 secondes" in text or "2 s" in text


def test_realtime_page_documents_the_three_commands_and_payloads() -> None:
    text = read(REALTIME)

    assert "realtime run" in text
    assert "realtime serve" in text
    assert "realtime check" in text
    assert "realtime-check" in text
    assert "realtime-run" in text
    assert "realtime-serve" in text
    assert "state_db_writable" in text
    assert "TB_ALLOW_LIVE_TRADING" in text
    assert "I_UNDERSTAND_THE_RISK" in text
    assert "client_order_id" in text


def test_realtime_page_states_what_is_not_proven() -> None:
    text = read(REALTIME)
    section = text.split("## 7. What is NOT proven", 1)[1]

    assert "WebSocket" in section
    assert "ASGI" in section
    assert "single operator token" in section
    assert "order book" in section
    assert "closed" in section
    assert "leverage" in section
    assert "single-writer" in section
    assert "NOT exercised against a real" in section


def test_every_relative_markdown_link_resolves() -> None:
    sources = (ARCHITECTURE, USAGE, REALTIME, README)
    missing: list[str] = []
    for source in sources:
        for target in markdown_link_targets(read(source)):
            if not (source.parent / target).resolve().is_file():
                missing.append(f"{source.relative_to(REPO_ROOT)} -> {target}")
    assert not missing, f"broken documentation links: {missing}"


def test_readme_links_the_realtime_page() -> None:
    readme = read(README)

    assert "docs/realtime.md" in readme
    assert "](docs/realtime.md)" in readme
    assert "## Documentation" in readme
    assert "## Démarrage rapide" in readme


# ---------------------------------------------------------------------------
# 9. the shipped example file carries no credential and no endpoint
# ---------------------------------------------------------------------------


def test_example_profiles_file_has_no_credential_and_no_endpoint() -> None:
    assert EXAMPLE_PROFILES.is_file(), "config/profiles.example.json is missing"
    text = read(EXAMPLE_PROFILES)
    lowered = text.lower()

    for key in FORBIDDEN_PROFILE_KEYS:
        assert key not in lowered, f"the example profiles file contains {key!r}"
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "wss://" not in lowered


def test_example_profiles_file_is_a_valid_profiles_document() -> None:
    document: dict[str, Any] = json.loads(read(EXAMPLE_PROFILES))

    assert set(document) == {"profiles", "realtime", "monitoring"}
    assert document["profiles"], "the example must declare at least one profile"
    for entry in document["profiles"]:
        assert entry["mode"] == "paper", "the shipped example never ships a live profile"
        assert "risk" in entry
    assert document["realtime"]["state_db"].startswith("data/")


def test_example_profiles_file_is_accepted_by_the_loader() -> None:
    from trading_platform.config import load_profiles

    profiles = load_profiles(EXAMPLE_PROFILES)

    assert [profile.id for profile in profiles] == ["btc-paper", "eth-paper"]
    assert all(profile.mode == "paper" for profile in profiles)


# ---------------------------------------------------------------------------
# the Makefile target and the README bullet stay additive
# ---------------------------------------------------------------------------


def test_makefile_declares_the_realtime_target() -> None:
    text = read(MAKEFILE)

    assert re.search(r"^realtime:", text, flags=re.MULTILINE)
    assert "make realtime" in text
    assert "realtime" in text.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert "config/profiles.example.json" in text


def test_makefile_check_target_is_untouched() -> None:
    text = read(MAKEFILE)

    assert "check: lint type-check test-cov" in text
    assert "$(PYTHON) -m pytest tests -q" in text
    assert "$(PYTHON) -m mypy src" in text


def test_usage_documents_the_realtime_commands() -> None:
    text = read(USAGE)

    for token in (
        "realtime run",
        "realtime serve",
        "realtime check",
        "profiles.example.json",
        "TB_ALLOW_LIVE_TRADING",
        "TB_OPERATOR_TOKEN",
        "state_db",
        "/api/kill-switch",
        "make realtime",
    ):
        assert token in text, f"usage.md does not document {token!r}"
    # the payloads are spelled out with their exact keys
    assert "realtime-check" in text
    assert "realtime-run" in text
    assert "realtime-serve" in text


def test_usage_keeps_the_tokens_the_existing_policy_asserts() -> None:
    text = read(USAGE)

    for token in (
        "| Cible |",
        "--data-file",
        "backtest_default.json",
        "docker build",
        "docker-test",
        "freqtrade_dryrun.json",
        "AppConfig",
        "testing-policy.md",
        "backtest",
        "walk-forward",
        "robustness",
        "monte-carlo",
        "make_freqtrade_strategy",
        "BasicStrategy",
        "BasicFreqtradeStrategy",
        "--strategy-path",
        'pip install -e ".[freqtrade]"',
        "freqtrade trade",
        "freqtrade_config.json",
    ):
        assert token in text, f"usage.md lost the documented token {token!r}"


@pytest.mark.parametrize("page", [ARCHITECTURE, USAGE, REALTIME])
def test_documentation_pages_are_utf8_and_not_empty(page: Path) -> None:
    text = read(page)

    assert text, f"{page.name} is empty"
    assert len(text.splitlines()) > 100, f"{page.name} looks truncated"


@pytest.mark.parametrize("page", [ARCHITECTURE, USAGE])
def test_the_legacy_documentation_pages_stay_french(page: Path) -> None:
    """The pages that predate the real-time delivery are French, and stay French."""
    text = read(page)

    # a French page of this size cannot contain zero accented words
    assert any(word in text for word in ("é", "è", "à", "ù", "ê", "ç"))
