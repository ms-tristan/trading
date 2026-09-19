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
TIMESFM_EXAMPLE_PROFILES = REPO_ROOT / "config" / "profiles.timesfm.example.json"

#: The three-command operational flow of a forecast profile, frozen by name:
#: download the candles, build the artifact, ask the guard, then start the engine.
FORECAST_FLOW_TARGETS = (
    "data-download",
    "forecast-bootstrap",
    "forecast-info",
    "realtime-forecast",
)

#: The recipe of every target that must **not** reach the network.  ``data
#: download`` is the only target of this delivery allowed to dial out, so any
#: other recipe containing this subcommand would break the offline promise of
#: the test-suite and of a locked-down machine.
NETWORK_SUBCOMMAND = "data download"

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
    "## 8. Profile lifecycle and candle history",
    "## 9. The shared platform wallet",
)

#: The guard block of the forecasting delivery, frozen by its heading **text**
#: rather than by its level.
#:
#: The delivery was planned with the guard as a top-level ``## 10.`` section, and
#: that exact title stays accepted below so a later promotion of the block keeps
#: satisfying this contract.  What shipped in ``docs/realtime.md`` is the same
#: content one level down, as ``### 3.1 The forecast startup guard``, placed
#: immediately after the safety model whose §3.4 funding check it mirrors.  The
#: heading is therefore matched at any level and the **content** below is what
#: this module actually pins: moving a documented paragraph between levels is an
#: editorial choice, silently dropping its contract is not.
FORECAST_GUARD_TITLE = "The forecast startup guard"

#: The title the delivery plan froze; accepted as an alias of the block above.
FORECAST_GUARD_SECTION = "## 10. The forecast startup guard"

#: A leading section number, e.g. ``3.1 `` in ``### 3.1 The forecast startup guard``.
_HEADING_NUMBER = re.compile(r"^(?:\d+(?:\.\d+)*\.?)\s+")

#: Routes the web-API table must enumerate.  The server is a pure JSON API: it
#: serves no HTML page and no static asset, so the table carries the API rows
#: only -- the read counterpart of the kill switch included, plus the candle
#: history, the two picker routes and the four profile lifecycle routes.
REALTIME_ROUTES = (
    "GET /api/health",
    "GET /api/profiles",
    "GET /api/profiles/{id}",
    "GET /api/profiles/{id}/equity",
    "GET /api/profiles/{id}/trades",
    "GET /api/profiles/{id}/orders",
    "GET /api/profiles/{id}/positions",
    "GET /api/profiles/{id}/metrics",
    "GET /api/profiles/{id}/candles",
    "GET /api/catalog",
    "GET /api/control",
    "GET /api/kill-switch",
    "POST /api/kill-switch",
    "POST /api/profiles",
    "POST /api/profiles/{id}/pause",
    "POST /api/profiles/{id}/resume",
    "DELETE /api/profiles/{id}",
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
        "# API-only layer 7",
        "# standalone Next.js dashboard",
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


def test_realtime_page_documents_the_api_only_404_and_the_nextjs_dashboard() -> None:
    """§5 describes a pure JSON API and a *separate* Next.js dashboard."""
    text = read(REALTIME)

    # Every path outside the API table -- `GET /` and `GET /static/{asset}`
    # included -- answers the documented JSON 404 payload.
    assert "not found: <path>" in text, (
        "docs/realtime.md must document the JSON 404 payload of a non-API path"
    )
    assert '{"error": "not found: <path>"}' in text
    assert "`GET /`" in text
    assert "`GET /static/{asset}`" in text
    # ... and the two removed rows are gone from the route table itself.
    assert "| `GET /` |" not in text, "the route table still advertises an HTML page"
    assert "| `GET /static/{asset}` |" not in text, (
        "the route table still advertises a static asset"
    )

    # The dashboard is an application of its own, polling the same JSON API.
    assert "separate Next.js application" in text
    assert "dashboard/" in text


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


# ---------------------------------------------------------------------------
# 10. the profile surface: lifecycle semantics, candle history, French pages
# ---------------------------------------------------------------------------


def test_realtime_page_documents_the_lifecycle_and_candle_rules() -> None:
    """§8 states the pause/delete/create semantics precisely, not by allusion."""
    text = read(REALTIME)
    assert "## 8. Profile lifecycle and candle history" in text
    section = text.split("## 8. Profile lifecycle and candle history", 1)[1]

    # pause: the entry gate closes, the open position stays supervised
    assert "opening new positions" in section
    assert "never** left unmanaged" in section
    assert "stop of the open position" in section
    # ... and the flag is durable, exposed by /api/control, never by ProfileSnapshot
    assert "meta" in section
    assert "GET /api/control" in section
    assert "frozen" in section
    assert 'status: "running"' in section
    # delete: flatten at market first, rewrite the file atomically, refuse the last
    assert "at market" in section
    assert "execution gateway" in section
    assert "last** profile" in section
    assert "atomically" in section
    assert "os.replace" in section
    # create: validated against the catalog, started immediately
    assert "catalog" in section
    assert "started" in section
    assert "409" in section
    # candles: a bounded window, served in both modes
    assert "1000" in section
    assert "realtime serve" in section
    # the documented error mapping of the four mutations
    for status in ("400", "409", "503"):
        assert status in section


def test_realtime_page_corrects_the_candle_honesty_note() -> None:
    """The old 'the store keeps no candle' limitation is corrected on the page."""
    text = read(REALTIME)
    section = text.split("## 8. Profile lifecycle and candle history", 1)[1]

    assert "**no** candle at all" in section
    assert "bounded candle history" in section
    # ... while the decision rule itself is *not* relaxed
    assert "skipped" in section


def test_architecture_documents_the_new_surface_in_french() -> None:
    """The French inventory gains the candles table, the two seams and the routes."""
    text = read(ARCHITECTURE)

    assert "### 4.12 Cycle de vie des profils, catalogue et historique de bougies" in text
    for token in (
        "`candles`",
        "`SCHEMA_VERSION`",
        "realtime/catalog.py",
        "realtime/control.py",
        "`MarketCatalog`",
        "`RuntimeProfileController`",
        "asyncio.run_coroutine_threadsafe",
        "GET /api/profiles/{id}/candles",
        "GET /api/catalog",
        "GET /api/control",
        "POST /api/profiles",
        "DELETE /api/profiles/{id}",
    ):
        assert token in text, f"architecture.md does not document {token!r}"


def test_usage_documents_the_new_endpoints_in_french() -> None:
    """The French usage page shows the new calls and the dashboard actions."""
    text = read(USAGE)

    assert "### 11.8 Cycle de vie des profils et historique de bougies" in text
    for token in (
        "/api/catalog",
        "/api/control",
        "/api/profiles/btc-paper/candles",
        "/api/profiles/btc-paper/pause",
        "/api/profiles/btc-paper/resume",
        "X-Operator-Token",
        "DELETE",
        "chandeliers japonais",
    ):
        assert token in text, f"usage.md does not document {token!r}"


# ---------------------------------------------------------------------------
# 11. the forecast surface: the profile key, the guard, the operational flow
# ---------------------------------------------------------------------------


def test_realtime_page_documents_the_forecast_profile_field() -> None:
    """The profile table must carry the ``forecast`` row with its whole contract.

    ``forecast`` is the one key that turns a ``timesfm`` profile from an inert
    strategy into a trading one, so the field table of §1 must state three
    things and nothing less: the field name as it is written in the JSON, the
    ``null`` default (which is what every pre-existing profile gets, unchanged),
    and the fail-loud contract -- a declared artifact that is missing, corrupt,
    stale or built for another symbol/timeframe refuses the profile at startup.
    """
    text = read(REALTIME)

    assert "| `forecast` |" in text, "docs/realtime.md does not carry the `forecast` profile row"
    row = text.split("| `forecast` |", 1)[1].split("\n", 1)[0]

    assert "artifact" in row.lower(), "the `forecast` row must say it is an artifact path"
    assert "`null`" in row, "the `forecast` row must document the `null` default"
    assert "startup" in row, "the `forecast` row must state the refusal happens at startup"
    for cause in ("missing", "corrupt", "stale"):
        assert cause in row, f"the `forecast` row does not name the {cause!r} refusal"
    assert "symbol/timeframe" in row, "the `forecast` row must cover the mismatch refusal"
    assert "`basic`" in row, "the `forecast` row must state that `basic` is untouched"

    # ... and the page really explains the mechanism behind the row.
    for token in (
        "forecast-bootstrap",
        "forecast-info --profiles",
        "realtime.features.resolve_profile_features",
        "check_profile_forecast",
        "resolve_features",
        "attach_features",
    ):
        assert token in text, f"docs/realtime.md does not document {token!r}"


def _forecast_guard_block(text: str) -> str:
    """Return the body of the guard block of ``docs/realtime.md``.

    The block is located by its heading **text** (``FORECAST_GUARD_TITLE``) at
    whatever level the page carries it, so the frozen ``## 10.`` title and the
    shipped ``### 3.1`` title both resolve to the same body.  The body runs to
    the next heading of the same or a higher level, which is what keeps the
    assertions below scoped to the guard rather than to the whole page.

    Raises:
        AssertionError: when no heading carries the frozen title.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("#"):
            continue
        level = len(line) - len(line.lstrip("#"))
        title = _HEADING_NUMBER.sub("", line[level:].strip())
        if title != FORECAST_GUARD_TITLE:
            continue
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.startswith("#"):
                following_level = len(following) - len(following.lstrip("#"))
                if following_level <= level:
                    break
            body.append(following)
        return "\n".join(body)
    raise AssertionError(
        f"docs/realtime.md carries no heading titled {FORECAST_GUARD_TITLE!r} "
        f"(expected {FORECAST_GUARD_SECTION!r} or its shipped subsection)"
    )


def test_realtime_page_has_the_forecast_startup_guard_block() -> None:
    """The guard owns a block of its own: its checks, its boundary, its refusal.

    The delivery plan reserved the top-level title ``## 10. The forecast startup
    guard`` for this content; the shipped page carries the very same content as
    ``### 3.1``, directly under the safety model whose funding check it mirrors.
    Both are accepted, and what is pinned is the *contract*, not the heading
    level: the block exists, it documents the four refusals in order, it states
    that the guard is mandatory, it quotes the two commands that make the
    refusal operable, and it says how an operator confirms the profile really
    trades instead of merely starting.
    """
    text = read(REALTIME)

    # --- the block exists, under either the frozen title or the shipped one
    assert FORECAST_GUARD_TITLE in text, (
        f"docs/realtime.md carries no {FORECAST_GUARD_TITLE!r} title "
        f"(frozen as {FORECAST_GUARD_SECTION!r})"
    )
    section = _forecast_guard_block(text)
    assert section.strip(), "the forecast guard block is empty"

    # the four checks, in order, each one a loud refusal
    for check in (
        "symbol mismatch",
        "timeframe mismatch",
        "staleness",
    ):
        assert check in section, f"the guard block does not document the {check!r} check"
    assert "ForecastArtifactError" in section
    # the guard is not advisory: it runs with `required=True` and a profile that
    # fails it is refused at startup, never started to run inert.
    assert "required=True" in section, (
        "the guard block must state that the guard runs with required=True"
    )
    assert "refused at startup" in section or "mandatory" in section, (
        "the guard block must state that a refused profile never starts"
    )
    assert "never started to run inert" in section or "run inert" in section, (
        "the guard block must state the failure it removes: an inert profile"
    )

    # the refusal is actionable: it names the rebuild and the pre-flight commands
    assert "trading forecast-build" in section
    assert "trading forecast-info" in section

    # the boundary semantics are pinned rather than left to the reader
    assert "usable_until" in section or "usable until" in section
    assert "min_lead" in section
    assert "forecast_age" in section
    assert "age_bound" in section or "age bound" in section

    # a profile that declares no forecast keeps the previous behaviour, and a
    # `timesfm` profile that declares none fails just as loudly as a stale one.
    assert "`basic`" in section
    assert "no `forecast` path" in section or "declares no forecast" in section

    # ... and the page states the observable proof that it trades, not starts.
    # "Started is not trading" belongs to the operational flow of §6.1 rather
    # than to the guard block itself, so it is pinned on the page.
    assert "n_trades" in text, "docs/realtime.md must say how real trading is confirmed"
    assert "open_positions" in text, (
        "docs/realtime.md must name the second observable of a trading profile"
    )


def test_usage_documents_the_forecast_operational_flow() -> None:
    """The usage page must show the flow an operator actually runs, command by command."""
    text = read(USAGE)

    for token in (
        "forecast-bootstrap",
        "--profiles",
        "--profile",
        "make forecast-flow",
        "make realtime-forecast",
        "make forecast-profile",
        "make data-download",
        "timesfm",
        '"forecast"',
    ):
        assert token in text, f"usage.md does not document {token!r}"

    # the three commands of the flow, in the order the operator runs them
    section = text.split("### 11.7.1 Operating a forecast profile: three commands", 1)
    assert len(section) == 2, "usage.md lost the dedicated forecast-operating section of §11.7.1"
    body = section[1]
    positions = [
        body.index("make data-download"),
        body.index("make forecast-profile"),
        body.index("make realtime-forecast"),
    ]
    assert positions == sorted(positions), (
        "usage.md must present the flow as download -> build -> start"
    )


def test_makefile_declares_the_forecast_flow() -> None:
    """The three-command flow exists as a target, and only one target dials out.

    The delivery makes the operational path a *flow*, not a paragraph: the
    targets below must exist, the aggregated one must depend on the three others
    in order, and -- the offline promise of the whole test-suite -- no recipe but
    ``data-download`` may run the network-using subcommand.
    """
    text = read(MAKEFILE)

    for target in FORECAST_FLOW_TARGETS:
        assert re.search(rf"^{re.escape(target)}:", text, flags=re.MULTILINE), (
            f"the Makefile has no {target!r} target"
        )
    assert re.search(r"^forecast-flow:", text, flags=re.MULTILINE), (
        "the Makefile has no aggregated `forecast-flow` target"
    )

    flow = text.split("\nforecast-flow:", 1)[1].split("\n", 1)[0]
    for target in FORECAST_FLOW_TARGETS:
        assert target in flow, f"`forecast-flow` does not depend on {target!r}"
    flow_positions = [flow.index(target) for target in FORECAST_FLOW_TARGETS]
    assert flow_positions == sorted(flow_positions), (
        "`forecast-flow` must run download -> build -> info -> realtime, in that order"
    )
    assert "download, bootstrap, verify, trade" in flow

    # every recipe but `data-download` stays offline
    recipes = re.findall(r"^([A-Za-z0-9_-]+):.*\n((?:\t.*\n)+)", text, flags=re.MULTILINE)
    assert recipes, "the Makefile carries no recipe at all"
    offenders = [
        name
        for name, body in recipes
        if NETWORK_SUBCOMMAND in body
        and name != "data-download"
        and "data-download" not in name
        and name != "forecast-flow"
    ]
    assert not offenders, f"these Makefile targets reach the network: {offenders}"


def test_example_profiles_opt_into_no_forecast() -> None:
    """The shipped example declares the key on every profile, and uses none of it.

    ``"forecast": null`` is the load-bearing detail: the field exists (so its
    absence from the file would fail the exact-key-set contract of
    ``tests/test_realtime_models.py``), and every profile leaves it at ``None``,
    so the example still describes two plain ``basic`` profiles whose behaviour
    is byte-for-byte the one they had before forecasting existed.
    """
    document: dict[str, Any] = json.loads(read(EXAMPLE_PROFILES))

    assert document["profiles"], "the example must declare at least one profile"
    for entry in document["profiles"]:
        assert "forecast" in entry, f"profile {entry['id']!r} does not carry the `forecast` key"
        assert entry["forecast"] is None, (
            f"profile {entry['id']!r} opts into a forecast; the shipped example opts into nothing"
        )
        assert entry["strategy"] != "timesfm", (
            f"profile {entry['id']!r} uses `timesfm` without an artifact and would be refused"
        )


def test_timesfm_example_profile_is_valid_and_declares_its_artifact() -> None:
    """The forecast example is a real, loadable profile -- not an illustration.

    It is the profile the operational flow points at, so it must parse through
    the project's own loader, declare the ``timesfm`` strategy and carry a
    non-null ``forecast`` path; anything less and the documented three commands
    would start an inert profile, which is the exact failure this delivery fixes.
    """
    assert TIMESFM_EXAMPLE_PROFILES.is_file(), "config/profiles.timesfm.example.json is missing"
    document: dict[str, Any] = json.loads(read(TIMESFM_EXAMPLE_PROFILES))
    assert set(document) == {"profiles", "realtime", "monitoring"}

    from trading_platform.config import load_profiles

    profiles = load_profiles(TIMESFM_EXAMPLE_PROFILES)

    assert profiles, "the forecast example declares no profile"
    for profile in profiles:
        assert profile.strategy == "timesfm", (
            f"profile {profile.id!r} of the forecast example is not a `timesfm` profile"
        )
        assert profile.forecast_artifact is not None, (
            f"profile {profile.id!r} declares no forecast artifact"
        )
        assert profile.mode == "paper", "the shipped example never ships a live profile"
        assert profile.timeframe in {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    # the shipped example still carries no credential-ish key
    lowered = read(TIMESFM_EXAMPLE_PROFILES).lower()
    for key in FORBIDDEN_PROFILE_KEYS:
        assert key not in lowered, f"the forecast example contains {key!r}"
