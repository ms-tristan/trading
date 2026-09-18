/*
 * Real-time engine monitoring dashboard (layer 7, wp9).
 *
 * Constraints honoured by this file:
 *   - plain JavaScript, no build tool, no library, no network call outside the
 *     same origin (every URL is relative);
 *   - polling of GET /api/profiles and GET /api/health every 2000 ms
 *     (REFRESH_MS constant, cadence documented in index.html);
 *   - equity curve drawn by hand in a canvas (no library), with screen pixel
 *     ratio (DPR) correction;
 *   - no loaded value is ever written with innerHTML: everything goes through
 *     textContent, so data can never become markup;
 *   - no exception ever escapes: a failed poll displays a banner.
 */
"use strict";

(function () {
  var REFRESH_MS = 2000;
  var MAX_TRADES = 10;
  var TOKEN_KEY = "tb.operator_token";

  var elements = {
    status: document.getElementById("platform-status"),
    version: document.getElementById("platform-version"),
    running: document.getElementById("profiles-running"),
    total: document.getElementById("profiles-total"),
    checkedAt: document.getElementById("checked-at"),
    banner: document.getElementById("banner"),
    profiles: document.getElementById("profiles"),
    token: document.getElementById("operator-token"),
    killEngage: document.getElementById("kill-engage"),
    killRelease: document.getElementById("kill-release"),
    killMessage: document.getElementById("kill-message")
  };

  var polling = false;

  /* ------------------------------------------------------------------ */
  /* DOM helpers (never innerHTML)                                       */
  /* ------------------------------------------------------------------ */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function badge(text, kind) {
    return el("span", "badge badge-" + kind, text);
  }

  function field(label, value) {
    var wrapper = el("div", "field");
    wrapper.appendChild(el("span", "field-label", label));
    wrapper.appendChild(el("span", "field-value", value));
    return wrapper;
  }

  function formatNumber(value, digits) {
    if (typeof value !== "number" || !isFinite(value)) {
      return "-";
    }
    return value.toFixed(digits === undefined ? 2 : digits);
  }

  function formatMoney(value) {
    return typeof value === "number" && isFinite(value) ? value.toFixed(2) : "-";
  }

  function formatTimestamp(value) {
    return typeof value === "string" && value ? value.replace("T", " ").slice(0, 19) : "-";
  }

  function formatLag(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds)) {
      return "-";
    }
    return seconds.toFixed(1) + " s";
  }

  /* ------------------------------------------------------------------ */
  /* error banner (never throws)                                         */
  /* ------------------------------------------------------------------ */

  function showBanner(message) {
    if (!elements.banner) {
      return;
    }
    elements.banner.textContent = message;
    elements.banner.classList.remove("hidden");
  }

  function hideBanner() {
    if (elements.banner) {
      elements.banner.classList.add("hidden");
    }
  }

  function getJson(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (response) {
      if (!response.ok) {
        throw new Error("HTTP " + response.status + " on " + url);
      }
      return response.json();
    });
  }

  /* ------------------------------------------------------------------ */
  /* operator token (sessionStorage only, never displayed)               */
  /* ------------------------------------------------------------------ */

  function readToken() {
    try {
      return window.sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function writeToken(value) {
    try {
      window.sessionStorage.setItem(TOKEN_KEY, value);
    } catch (error) {
      /* storage unavailable: the token is simply not remembered */
    }
  }

  /* ------------------------------------------------------------------ */
  /* global kill switch                                                  */
  /* ------------------------------------------------------------------ */

  function setKillMessage(message) {
    if (elements.killMessage) {
      elements.killMessage.textContent = message;
    }
  }

  function postKillSwitch(engage) {
    var token = readToken();
    if (!token) {
      setKillMessage("operator token required");
      return Promise.resolve();
    }
    return fetch("/api/kill-switch", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Operator-Token": token
      },
      body: JSON.stringify({
        engage: engage,
        reason: engage ? "dashboard: emergency stop" : "dashboard: resume"
      })
    })
      .then(function (response) {
        if (response.status === 403) {
          setKillMessage("mutations disabled");
          return null;
        }
        return response.json().then(function (payload) {
          if (!response.ok) {
            setKillMessage("failed: " + (payload && payload.error ? payload.error : response.status));
            return null;
          }
          setKillMessage(payload && payload.kill_switch ? "kill switch engaged" : "kill switch released");
          return null;
        });
      })
      .catch(function () {
        setKillMessage("request failed");
      });
  }

  /* ------------------------------------------------------------------ */
  /* equity curve: canvas drawn by hand, DPR aware                       */
  /* ------------------------------------------------------------------ */

  function drawEquity(canvas, points) {
    var ratio = window.devicePixelRatio || 1;
    var width = canvas.clientWidth || 320;
    var height = canvas.clientHeight || 96;
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    var context = canvas.getContext("2d");
    if (!context) {
      return;
    }
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);

    var series = [];
    var index;
    for (index = 0; index < points.length; index += 1) {
      var value = points[index] ? points[index].equity : null;
      if (typeof value === "number" && isFinite(value)) {
        series.push(value);
      }
    }
    if (series.length < 2) {
      context.fillStyle = "#8b97a8";
      context.font = "12px sans-serif";
      context.fillText("equity curve unavailable", 8, height / 2);
      return;
    }

    var minimum = series[0];
    var maximum = series[0];
    for (index = 1; index < series.length; index += 1) {
      minimum = Math.min(minimum, series[index]);
      maximum = Math.max(maximum, series[index]);
    }
    var span = maximum - minimum;
    if (span <= 0) {
      span = 1;
    }
    var step = series.length > 1 ? (width - 2) / (series.length - 1) : 0;

    context.strokeStyle = "#4ea1ff";
    context.lineWidth = 1.5;
    context.beginPath();
    for (index = 0; index < series.length; index += 1) {
      var x = 1 + index * step;
      var y = height - 2 - ((series[index] - minimum) / span) * (height - 12);
      if (index === 0) {
        context.moveTo(x, y);
      } else {
        context.lineTo(x, y);
      }
    }
    context.stroke();

    context.fillStyle = "#8b97a8";
    context.font = "11px sans-serif";
    context.fillText(maximum.toFixed(2), 4, 12);
    context.fillText(minimum.toFixed(2), 4, height - 4);
  }

  /* ------------------------------------------------------------------ */
  /* rendering of one profile                                            */
  /* ------------------------------------------------------------------ */

  function statusKind(status) {
    if (status === "running") {
      return "ok";
    }
    if (status === "degraded" || status === "starting") {
      return "warn";
    }
    if (status === "halted" || status === "error") {
      return "bad";
    }
    return "unknown";
  }

  function renderPositions(list) {
    var section = el("section", "block");
    section.appendChild(el("h3", null, "Open positions"));
    if (!list.length) {
      section.appendChild(el("p", "muted", "no open position"));
      return section;
    }
    var table = el("table", "grid");
    var head = el("tr", null, null);
    ["symbol", "side", "quantity", "average price", "unrealized PnL"].forEach(function (label) {
      head.appendChild(el("th", null, label));
    });
    table.appendChild(head);
    list.forEach(function (position) {
      var row = el("tr", null, null);
      row.appendChild(el("td", null, position.symbol));
      row.appendChild(el("td", null, position.direction));
      row.appendChild(el("td", null, formatNumber(position.quantity, 6)));
      row.appendChild(el("td", null, formatMoney(position.average_price)));
      row.appendChild(el("td", null, formatMoney(position.unrealized_pnl)));
      table.appendChild(row);
    });
    section.appendChild(table);
    return section;
  }

  function renderTrades(list) {
    var section = el("section", "block");
    section.appendChild(el("h3", null, "Last 10 trades"));
    if (!list.length) {
      section.appendChild(el("p", "muted", "no closed trade"));
      return section;
    }
    var table = el("table", "grid");
    var head = el("tr", null, null);
    ["exit time", "side", "size", "exit price", "PnL"].forEach(function (label) {
      head.appendChild(el("th", null, label));
    });
    table.appendChild(head);
    list.slice(0, MAX_TRADES).forEach(function (trade) {
      var row = el("tr", null, null);
      row.appendChild(el("td", null, formatTimestamp(trade.exit_time)));
      row.appendChild(el("td", null, trade.direction));
      row.appendChild(el("td", null, formatNumber(trade.size, 6)));
      row.appendChild(el("td", null, formatMoney(trade.exit_price)));
      row.appendChild(el("td", null, formatMoney(trade.pnl)));
      table.appendChild(row);
    });
    section.appendChild(table);
    return section;
  }

  function profileCard(profile) {
    var card = el("article", "card");
    var head = el("header", "card-head");
    head.appendChild(el("h2", null, profile.profile_id));
    head.appendChild(badge(profile.status, statusKind(profile.status)));
    head.appendChild(badge(profile.mode, profile.mode === "live" ? "bad" : "ok"));
    card.appendChild(head);

    var facts = el("div", "facts");
    facts.appendChild(field("symbol", profile.symbol));
    facts.appendChild(field("timeframe", profile.timeframe));
    facts.appendChild(field("strategy", profile.strategy));
    facts.appendChild(field("equity", formatMoney(profile.equity)));
    facts.appendChild(field("cash", formatMoney(profile.cash)));
    facts.appendChild(field("position value", formatMoney(profile.position_value)));
    facts.appendChild(field("return", formatNumber(profile.total_return * 100, 2) + " %"));
    facts.appendChild(field("trades", profile.n_trades));
    facts.appendChild(field("positions", profile.open_positions));
    var health = profile.health || {};
    facts.appendChild(field("last candle", formatTimestamp(health.last_candle_at)));
    facts.appendChild(field("lag", formatLag(health.lag_seconds)));
    facts.appendChild(field("reconnects", health.reconnect_count));
    card.appendChild(facts);

    if (health.last_error) {
      var error = el("p", "error", "last error: " + health.last_error);
      card.appendChild(error);
    }

    var counters = health.counters || {};
    var line = el("p", "muted", null);
    line.textContent =
      "candles " +
      (counters.candles_processed || 0) +
      " - orders " +
      (counters.orders_submitted || 0) +
      " - filled " +
      (counters.orders_filled || 0) +
      " - rejected " +
      (counters.orders_rejected || 0) +
      " - risk " +
      (counters.risk_rejections || 0);
    card.appendChild(line);

    var canvas = el("canvas", "equity");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "equity curve for " + profile.profile_id);
    card.appendChild(canvas);

    var detail = el("div", "detail");
    detail.appendChild(el("p", "muted", "loading detail..."));
    card.appendChild(detail);

    return { card: card, canvas: canvas, detail: detail, points: [] };
  }

  function renderDetail(container, positions, trades) {
    container.replaceChildren();
    container.appendChild(renderPositions(positions));
    container.appendChild(renderTrades(trades));
  }

  function loadDetail(entry, profileId) {
    var requests = [
      getJson("/api/profiles/" + encodeURIComponent(profileId) + "/equity"),
      getJson("/api/profiles/" + encodeURIComponent(profileId) + "/positions"),
      getJson("/api/profiles/" + encodeURIComponent(profileId) + "/trades")
    ];
    return Promise.all(requests)
      .then(function (answers) {
        var equity = answers[0] || {};
        var positions = answers[1] || {};
        var trades = answers[2] || {};
        entry.points = Array.isArray(equity.points) ? equity.points : [];
        renderDetail(
          entry.detail,
          Array.isArray(positions.positions) ? positions.positions : [],
          Array.isArray(trades.trades) ? trades.trades : []
        );
      })
      .catch(function () {
        entry.detail.replaceChildren();
        entry.detail.appendChild(el("p", "muted", "detail unavailable"));
      });
  }

  function renderPlatform(health) {
    if (elements.status) {
      var kind = health.status === "degraded" || health.kill_switch ? "bad" : "ok";
      elements.status.className = "badge badge-" + kind;
      elements.status.textContent = health.kill_switch
        ? "global kill switch engaged"
        : health.status === "degraded"
          ? "degraded"
          : "operational";
    }
    if (elements.version) {
      elements.version.textContent = health.version || "-";
    }
    if (elements.running) {
      elements.running.textContent = String(health.profiles_running);
    }
    if (elements.total) {
      elements.total.textContent = String(health.profiles_total);
    }
    if (elements.checkedAt) {
      elements.checkedAt.textContent = formatTimestamp(health.checked_at);
    }
  }

  function renderProfiles(payload) {
    var profiles = payload && Array.isArray(payload.profiles) ? payload.profiles : [];
    if (!elements.profiles) {
      return [];
    }
    elements.profiles.replaceChildren();
    if (!profiles.length) {
      elements.profiles.appendChild(el("p", "muted", "no profile configured"));
      return [];
    }
    var entries = [];
    profiles.forEach(function (profile) {
      var entry = profileCard(profile);
      entries.push({ entry: entry, profileId: profile.profile_id });
      elements.profiles.appendChild(entry.card);
    });
    return entries;
  }

  /* ------------------------------------------------------------------ */
  /* polling loop                                                        */
  /* ------------------------------------------------------------------ */

  function refresh() {
    if (polling) {
      return Promise.resolve();
    }
    polling = true;
    return Promise.all([getJson("/api/health"), getJson("/api/profiles")])
      .then(function (answers) {
        hideBanner();
        renderPlatform(answers[0] || {});
        var entries = renderProfiles(answers[1] || {});
        return Promise.all(
          entries.map(function (item) {
            return loadDetail(item.entry, item.profileId);
          })
        ).then(function () {
          entries.forEach(function (item) {
            drawEquity(item.entry.canvas, item.entry.points);
          });
        });
      })
      .catch(function (error) {
        showBanner(
          "polling failed: " + (error && error.message ? error.message : "unknown error")
        );
      })
      .then(function () {
        polling = false;
      });
  }

  function bindKillSwitch() {
    if (elements.token) {
      elements.token.value = readToken();
      elements.token.addEventListener("change", function () {
        writeToken(elements.token.value.trim());
      });
    }
    if (elements.killEngage) {
      elements.killEngage.addEventListener("click", function () {
        rememberTypedToken();
        postKillSwitch(true);
      });
    }
    if (elements.killRelease) {
      elements.killRelease.addEventListener("click", function () {
        rememberTypedToken();
        postKillSwitch(false);
      });
    }
  }

  /* The token typed just before the click is remembered before sending: the
     button does not wait for the "change" event (which only fires when the
     field loses focus) to use the value that was entered. */
  function rememberTypedToken() {
    if (elements.token && elements.token.value) {
      writeToken(elements.token.value.trim());
    }
  }

  function start() {
    bindKillSwitch();
    refresh();
    window.setInterval(refresh, REFRESH_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
