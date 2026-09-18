/*
 * Tableau de bord de supervision du moteur temps reel (couche 7, wp9).
 *
 * Contraintes tenues par ce fichier :
 *   - JavaScript natif, aucun outil de build, aucune bibliotheque, aucun
 *     appel reseau hors de la meme origine (toutes les URL sont relatives) ;
 *   - sondage de GET /api/profiles et GET /api/health toutes les 2000 ms
 *     (constante REFRESH_MS, cadence documentee dans index.html) ;
 *   - courbe d'equity tracee a la main dans un canvas (aucune bibliotheque),
 *     avec correction du ratio de pixels de l'ecran (DPR) ;
 *   - aucune valeur Chargee n'est ecrite avec innerHTML : tout passe par
 *     textContent, donc une donnee ne peut jamais devenir du balisage ;
 *   - aucune exception ne remonte : un echec de sondage affiche une banniere.
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
  /* utilitaires DOM (jamais innerHTML)                                  */
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
  /* banniere d'erreur (jamais d'exception)                              */
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
        throw new Error("HTTP " + response.status + " sur " + url);
      }
      return response.json();
    });
  }

  /* ------------------------------------------------------------------ */
  /* jeton operateur (sessionStorage uniquement, jamais affiche)          */
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
      /* stockage indisponible : le jeton reste simplement non memorise */
    }
  }

  /* ------------------------------------------------------------------ */
  /* interrupteur d'arret global                                         */
  /* ------------------------------------------------------------------ */

  function setKillMessage(message) {
    if (elements.killMessage) {
      elements.killMessage.textContent = message;
    }
  }

  function postKillSwitch(engage) {
    var token = readToken();
    if (!token) {
      setKillMessage("jeton operateur requis");
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
        reason: engage ? "tableau de bord: arret d'urgence" : "tableau de bord: reprise"
      })
    })
      .then(function (response) {
        if (response.status === 403) {
          setKillMessage("mutations disabled");
          return null;
        }
        return response.json().then(function (payload) {
          if (!response.ok) {
            setKillMessage("echec: " + (payload && payload.error ? payload.error : response.status));
            return null;
          }
          setKillMessage(payload && payload.kill_switch ? "arret engage" : "arret leve");
          return null;
        });
      })
      .catch(function () {
        setKillMessage("requete impossible");
      });
  }

  /* ------------------------------------------------------------------ */
  /* courbe d'equity : canvas dessine a la main, conscient du DPR         */
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
      context.fillText("courbe indisponible", 8, height / 2);
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
  /* rendu d'un profil                                                   */
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
    section.appendChild(el("h3", null, "Positions ouvertes"));
    if (!list.length) {
      section.appendChild(el("p", "muted", "aucune position ouverte"));
      return section;
    }
    var table = el("table", "grid");
    var head = el("tr", null, null);
    ["symbole", "cote", "quantite", "prix moyen", "PnL latent"].forEach(function (label) {
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
    section.appendChild(el("h3", null, "10 derniers trades"));
    if (!list.length) {
      section.appendChild(el("p", "muted", "aucun trade cloture"));
      return section;
    }
    var table = el("table", "grid");
    var head = el("tr", null, null);
    ["sortie", "cote", "taille", "prix sortie", "PnL"].forEach(function (label) {
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
    facts.appendChild(field("symbole", profile.symbol));
    facts.appendChild(field("unite de temps", profile.timeframe));
    facts.appendChild(field("strategie", profile.strategy));
    facts.appendChild(field("equity", formatMoney(profile.equity)));
    facts.appendChild(field("cash", formatMoney(profile.cash)));
    facts.appendChild(field("valeur position", formatMoney(profile.position_value)));
    facts.appendChild(field("variation", formatNumber(profile.total_return * 100, 2) + " %"));
    facts.appendChild(field("trades", profile.n_trades));
    facts.appendChild(field("positions", profile.open_positions));
    var health = profile.health || {};
    facts.appendChild(field("derniere bougie", formatTimestamp(health.last_candle_at)));
    facts.appendChild(field("retard", formatLag(health.lag_seconds)));
    facts.appendChild(field("reconnexions", health.reconnect_count));
    card.appendChild(facts);

    if (health.last_error) {
      var error = el("p", "error", "derniere erreur: " + health.last_error);
      card.appendChild(error);
    }

    var counters = health.counters || {};
    var line = el("p", "muted", null);
    line.textContent =
      "bougies " +
      (counters.candles_processed || 0) +
      " - ordres " +
      (counters.orders_submitted || 0) +
      " - remplis " +
      (counters.orders_filled || 0) +
      " - rejetes " +
      (counters.orders_rejected || 0) +
      " - risque " +
      (counters.risk_rejections || 0);
    card.appendChild(line);

    var canvas = el("canvas", "equity");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "courbe d'equity de " + profile.profile_id);
    card.appendChild(canvas);

    var detail = el("div", "detail");
    detail.appendChild(el("p", "muted", "chargement du detail..."));
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
        entry.detail.appendChild(el("p", "muted", "detail indisponible"));
      });
  }

  function renderPlatform(health) {
    if (elements.status) {
      var kind = health.status === "degraded" || health.kill_switch ? "bad" : "ok";
      elements.status.className = "badge badge-" + kind;
      elements.status.textContent = health.kill_switch
        ? "arret global engage"
        : health.status === "degraded"
          ? "degrade"
          : "operationnel";
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
      elements.profiles.appendChild(el("p", "muted", "aucun profil configure"));
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
  /* boucle de sondage                                                   */
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
          "sondage impossible: " + (error && error.message ? error.message : "erreur inconnue")
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

  /* Le jeton tape juste avant le clic est memorise avant l'envoi : le bouton
     n'attend pas l'evenement "change" (qui ne se declenche qu'a la perte du
     focus) pour utiliser la valeur saisie. */
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
