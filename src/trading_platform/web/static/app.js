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
  /* The polling cadence comes from the page itself (`data-refresh-seconds` on
     <body>, written by the server from `monitoring.refresh_seconds`), so the
     documented configuration knob actually drives the dashboard: it used to be a
     constant that ignored the configuration entirely. */
  var DEFAULT_REFRESH_SECONDS = 2;
  var MAX_TRADES = 10;
  var TOKEN_KEY = "tb.operator_token";

  function refreshSeconds() {
    var raw = document.body ? document.body.getAttribute("data-refresh-seconds") : null;
    var seconds = parseFloat(raw);
    return isFinite(seconds) && seconds >= 0.5 ? seconds : DEFAULT_REFRESH_SECONDS;
  }

  var REFRESH_MS = Math.round(refreshSeconds() * 1000);

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

  /* ------------------------------------------------------------------ */
  /* one card per profile, built once and updated in place               */
  /* ------------------------------------------------------------------ */

  /* The cards are *not* rebuilt on every poll.  The first version called
     `replaceChildren()` on the container every cycle, which destroyed and
     recreated every card -- and with it the equity canvas and the detail block,
     reset to "loading detail...".  On a fast local link that was an invisible
     flash; on a slower one the cards spent most of each 2 s cycle blank, which is
     exactly what "the page keeps reloading and shows no data" looks like.  A card
     is now created once, keyed by profile id, and only its text nodes change. */

  var cardsById = {};

  function createField(label) {
    var wrapper = el("div", "field");
    wrapper.appendChild(el("span", "field-label", label));
    var valueNode = el("span", "field-value", "-");
    wrapper.appendChild(valueNode);
    return { node: wrapper, valueNode: valueNode };
  }

  function setField(fieldHandle, value) {
    var text = String(value === undefined || value === null || value === "" ? "-" : value);
    if (fieldHandle.valueNode.textContent !== text) {
      fieldHandle.valueNode.textContent = text;
    }
  }

  function createCard(profile) {
    var card = el("article", "card");

    var head = el("header", "card-head");
    head.appendChild(el("h2", null, profile.profile_id));
    var statusBadge = badge(profile.status, statusKind(profile.status));
    var modeBadge = badge(profile.mode, profile.mode === "live" ? "bad" : "ok");
    head.appendChild(statusBadge);
    head.appendChild(modeBadge);
    card.appendChild(head);

    var fields = {
      symbol: createField("symbol"),
      timeframe: createField("timeframe"),
      strategy: createField("strategy"),
      equity: createField("equity"),
      cash: createField("cash"),
      positionValue: createField("position value"),
      totalReturn: createField("return"),
      trades: createField("trades"),
      positions: createField("positions"),
      lastCandle: createField("last candle"),
      lag: createField("lag"),
      reconnects: createField("reconnects")
    };
    var facts = el("div", "facts");
    Object.keys(fields).forEach(function (key) {
      facts.appendChild(fields[key].node);
    });
    card.appendChild(facts);

    /* the error line is part of the card from the start: it is shown or hidden,
       never inserted, so a profile that recovers does not shift the layout */
    var errorLine = el("p", "error hidden", "");
    card.appendChild(errorLine);

    var countersLine = el("p", "muted", "");
    card.appendChild(countersLine);

    var canvas = el("canvas", "equity");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "equity curve for " + profile.profile_id);
    card.appendChild(canvas);

    var detail = el("div", "detail");
    detail.appendChild(el("p", "muted", "loading detail..."));
    card.appendChild(detail);

    return {
      card: card,
      statusBadge: statusBadge,
      modeBadge: modeBadge,
      fields: fields,
      errorLine: errorLine,
      countersLine: countersLine,
      canvas: canvas,
      detail: detail,
      points: [],
      pointsSignature: null,
      detailLoaded: false
    };
  }

  function updateCard(handle, profile) {
    var health = profile.health || {};

    handle.statusBadge.className = "badge badge-" + statusKind(profile.status);
    handle.statusBadge.textContent = String(profile.status);
    handle.modeBadge.className = "badge badge-" + (profile.mode === "live" ? "bad" : "ok");
    handle.modeBadge.textContent = String(profile.mode);

    setField(handle.fields.symbol, profile.symbol);
    setField(handle.fields.timeframe, profile.timeframe);
    setField(handle.fields.strategy, profile.strategy);
    setField(handle.fields.equity, formatMoney(profile.equity));
    setField(handle.fields.cash, formatMoney(profile.cash));
    setField(handle.fields.positionValue, formatMoney(profile.position_value));
    setField(handle.fields.totalReturn, formatNumber(profile.total_return * 100, 2) + " %");
    setField(handle.fields.trades, profile.n_trades);
    setField(handle.fields.positions, profile.open_positions);
    setField(handle.fields.lastCandle, formatTimestamp(health.last_candle_at));
    setField(handle.fields.lag, formatLag(health.lag_seconds));
    setField(handle.fields.reconnects, health.reconnect_count);

    if (health.last_error) {
      handle.errorLine.textContent = "last error: " + health.last_error;
      handle.errorLine.classList.remove("hidden");
    } else {
      handle.errorLine.textContent = "";
      handle.errorLine.classList.add("hidden");
    }

    var counters = health.counters || {};
    handle.countersLine.textContent =
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

  function renderDetail(container, positions, trades) {
    container.replaceChildren();
    container.appendChild(renderPositions(positions));
    container.appendChild(renderTrades(trades));
  }

  function loadDetail(handle, profileId) {
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
        handle.points = Array.isArray(equity.points) ? equity.points : [];
        handle.detailLoaded = true;
        renderDetail(
          handle.detail,
          Array.isArray(positions.positions) ? positions.positions : [],
          Array.isArray(trades.trades) ? trades.trades : []
        );
      })
      .catch(function () {
        /* Keep whatever was last displayed: blanking a card because one request
           failed is what made the page look empty.  The banner carries the error. */
        if (!handle.detailLoaded) {
          handle.detail.replaceChildren();
          handle.detail.appendChild(el("p", "muted", "detail unavailable"));
        }
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
    var container = elements.profiles;
    if (!container) {
      return [];
    }
    if (!profiles.length) {
      Object.keys(cardsById).forEach(function (id) {
        container.removeChild(cardsById[id].card);
        delete cardsById[id];
      });
      if (!container.querySelector(".empty")) {
        container.replaceChildren();
        container.appendChild(el("p", "muted empty", "no profile configured"));
      }
      return [];
    }
    /* the "loading profiles..." placeholder of index.html and the empty state:
       both are removed once real cards exist, and neither is ever re-added while
       the list has content */
    Array.prototype.slice
      .call(container.querySelectorAll(".placeholder, .empty"))
      .forEach(function (node) {
        container.removeChild(node);
      });
    var entries = [];
    var seen = {};
    profiles.forEach(function (profile) {
      var id = profile.profile_id;
      seen[id] = true;
      var handle = cardsById[id];
      var fresh = false;
      if (!handle) {
        handle = createCard(profile);
        cardsById[id] = handle;
        container.appendChild(handle.card);
        fresh = true;
      }
      updateCard(handle, profile);
      entries.push({ entry: handle, profileId: id, fresh: fresh });
    });
    Object.keys(cardsById).forEach(function (id) {
      if (!seen[id]) {
        container.removeChild(cardsById[id].card);
        delete cardsById[id];
      }
    });
    return entries;
  }

  /* ------------------------------------------------------------------ */
  /* polling loop                                                        */
  /* ------------------------------------------------------------------ */

  function refresh() {
    if (polling) {
      /* A slow link must not pile requests up: the next tick is skipped while one
         cycle is still in flight. */
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
            /* the canvas is only redrawn when the curve it shows really changed */
            var signature = equitySignature(item.entry.points);
            if (!item.fresh && signature === item.entry.pointsSignature) {
              return;
            }
            item.entry.pointsSignature = signature;
            drawEquity(item.entry.canvas, item.entry.points);
          });
        });
      })
      .catch(function (error) {
        /* The last rendered values stay on screen: an unreachable API is shown as
           a banner, never as an empty page. */
        showBanner(
          "polling failed: " + (error && error.message ? error.message : "unknown error")
        );
      })
      .then(function () {
        polling = false;
      });
  }

  function equitySignature(points) {
    if (!points || !points.length) {
      return "empty";
    }
    var last = points[points.length - 1];
    return points.length + ":" + String(last.timestamp) + ":" + String(last.equity);
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
