"use strict";
(function () {
  const MAX_LINES = 300;
  const STALE_MS = 3500;
  const DEPTH_CAP = 180;    // 1 Hz depth samples => 3-minute history window
  const ALERT_DEPTH_M = 5;  // display-only shallow-water alert threshold (amber)

  // Manual-field range table mirroring the server's _UPDATE_RANGES (client-side pre-check only).
  const RANGES = {
    lat: [-90, 90], lon: [-180, 180], sog_kn: [0, null], cog_deg: [0, 360],
    heading_true_deg: [0, 360], heading_mag_deg: [0, 360], mag_variation_deg: [-180, 180],
    altitude_m: [null, null], fix_quality: [0, null], satellites: [0, null], hdop: [0, null],
    stw_kn: [0, 100], depth_m: [0, 12000], rot_dpm: [-720, 720], wind_speed_kn: [0, 200],
    wind_dir_deg: [0, 360], sea_state: [0, 9], rudder_angle_deg: [-45, 45], set_deg: [0, 360],
    drift_kn: [0, 100],
  };
  // Fields the Save-as-defaults endpoint accepts (server allow-list).
  const INITIAL_FIELDS = ["stw_kn","depth_m","rot_dpm","wind_speed_kn","wind_dir_deg","sea_state","rudder_angle_deg","set_deg","drift_kn"];

  const SEA_STATES = [
    "0 — Calm (glassy)","1 — Calm (rippled)","2 — Smooth","3 — Slight","4 — Moderate",
    "5 — Rough","6 — Very rough","7 — High","8 — Very high","9 — Phenomenal",
  ];

  const $ = (id) => document.getElementById(id);
  function el(tag, cls, txt) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }

  const toastEl = $("toast");
  function toast(text) {
    toastEl.textContent = text;
    toastEl.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => toastEl.classList.remove("show"), 1800);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied: " + text);
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.focus(); ta.select();
      try { document.execCommand("copy"); toast("Copied: " + text); }
      catch (e2) { toast("Copy failed — select manually"); }
      document.body.removeChild(ta);
    }
  }

  // ---- shared state ----
  const panes = {};                 // channel_id -> stream pane state
  let channelMeta = {};             // channel_id -> {role, talker}
  let channelOrder = [];            // channel_id[]
  let aggState = null;              // consolidated (aggregate-tap) pane state, or null
  let cfg = null;                   // last /api/config
  let lastHealth = null;            // last health event
  let lastState = null;             // last state event
  let lastStateTs = 0;
  let statePending = false;
  const depthHistory = [];          // decimated 1 Hz depth samples (cap DEPTH_CAP)
  let lastDepthSampleTs = 0;
  const ackedAlerts = new Set();    // alert keys the operator acknowledged (auto-clears -> re-arms)
  let alertsSilenced = false;       // cosmetic silence toggle (no audio in this app)
  const alertFlags = { depthLow: false }; // state-frame-derived flags merged into applyHealth alerts
  const alertFirstSeen = {};        // alert key -> first-seen ms (row age)
  let activeTab = "conning";
  let diagTimer = null, secTimer = null;

  // colourblind-safe source parse (returns tag text + css class)
  function parseSource(src) {
    const s = String(src || "").toUpperCase();
    if (s.indexOf("LIVE") === 0) return { tag: "LIVE", cls: "src-live" };
    if (s === "SIM") return { tag: "SIM", cls: "src-sim" };
    return { tag: "OFF", cls: "src-off" };
  }
  function channelSourceByRole(role) {
    if (!lastHealth || !Array.isArray(lastHealth.channels)) return "OFF";
    for (const c of lastHealth.channels) {
      const meta = channelMeta[c.channel_id];
      if (meta && meta.role === role) return c.source;
    }
    return "OFF";
  }

  // number formatting helpers
  function num(v, d) { return (v == null || !Number.isFinite(Number(v))) ? "---" : Number(v).toFixed(d == null ? 1 : d); }
  function fmtLat(lat) {
    if (lat == null || !Number.isFinite(Number(lat))) return "---";
    const v = Number(lat), h = v >= 0 ? "N" : "S", a = Math.abs(v);
    const d = Math.floor(a), m = (a - d) * 60;
    return d + "° " + m.toFixed(3) + "′ " + h;
  }
  function fmtLon(lon) {
    if (lon == null || !Number.isFinite(Number(lon))) return "---";
    const v = Number(lon), h = v >= 0 ? "E" : "W", a = Math.abs(v);
    const d = Math.floor(a), m = (a - d) * 60;
    return d + "° " + m.toFixed(3) + "′ " + h;
  }
  function fmtUtc(iso) {
    if (!iso) return "--:--:--";
    const t = String(iso);
    const m = t.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : t;
  }
  // time-to-go seconds -> "H:MM" (autopilot readout)
  function fmtTtg(sec) {
    const s = Number(sec);
    if (!Number.isFinite(s) || s < 0) return "--:--";
    const total = Math.round(s / 60);
    const h = Math.floor(total / 60), m = total % 60;
    return h + ":" + (m < 10 ? "0" : "") + m;
  }
  // alert-row age: whole seconds -> compact "Ns" / "Nm" / "Nh"
  function fmtAge(s) {
    if (!Number.isFinite(s) || s < 0) return "0s";
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m";
    return Math.floor(s / 3600) + "h";
  }

  /* =====================================================================
   *  TABS
   * ===================================================================== */
  $("tabbar").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".tab");
    if (!btn) return;
    showTab(btn.dataset.view);
  });
  function showTab(view) {
    activeTab = view;
    for (const b of document.querySelectorAll(".tab")) b.classList.toggle("active", b.dataset.view === view);
    for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.id === "view-" + view);
    // per-tab pollers
    stopDiagPoll(); stopSecPoll();
    if (view === "maintenance") startDiagPoll();
    if (view === "config") { refreshInputs(); }
    if (view === "security") { startSecPoll(); }
  }

  /* =====================================================================
   *  CONNING (state SSE driven, rAF-throttled)
   * ===================================================================== */
  // Reusable radial tick-ring builder (shared by the compass rose, the wind rose, and the
  // Phase-3 dials). Draws minor/major ticks from r1 inward and, when `labels` is supplied,
  // a text label at each major tick. `majorEvery` is a degree modulus (0 => no majors).
  // `labels` = { r, fn(deg) -> string } or null. No-ops on a missing group so a stripped/
  // renamed target downgrades to a blank dial instead of throwing at load time.
  function buildDialTicks(group, cx, cy, r1, rMinor, rMajor, step, majorEvery, labels) {
    if (!group) return;
    const NS = "http://www.w3.org/2000/svg";
    for (let deg = 0; deg < 360; deg += step) {
      const major = majorEvery > 0 && deg % majorEvery === 0;
      const r2 = major ? rMajor : rMinor;
      const a = (deg - 90) * Math.PI / 180;
      const line = document.createElementNS(NS, "line");
      line.setAttribute("x1", (cx + r1 * Math.cos(a)).toFixed(1));
      line.setAttribute("y1", (cy + r1 * Math.sin(a)).toFixed(1));
      line.setAttribute("x2", (cx + r2 * Math.cos(a)).toFixed(1));
      line.setAttribute("y2", (cy + r2 * Math.sin(a)).toFixed(1));
      line.setAttribute("stroke", major ? "#7d8895" : "#1a2330");
      line.setAttribute("stroke-width", major ? "1.5" : "1");
      group.appendChild(line);
      if (major && labels) {
        const t = document.createElementNS(NS, "text");
        const rr = labels.r != null ? labels.r : 70;
        const lx = (cx + rr * Math.cos(a)).toFixed(1);
        const ly = (cy + rr * Math.sin(a) + 4).toFixed(1);
        t.setAttribute("x", lx);
        t.setAttribute("y", ly);
        t.setAttribute("fill", "#dde4ea");
        t.setAttribute("font-size", "10");
        t.setAttribute("font-family", "monospace");
        t.setAttribute("text-anchor", "middle");
        t.textContent = labels.fn(deg);
        // "floating" labels: on a dial whose card rotates with heading, stash each label's
        // anchor so repaint can counter-rotate it (rotate +hdg about its own point) → the
        // numeral stays upright at every heading while still riding the card to its bearing.
        if (labels.floating) { t.dataset.fx = lx; t.dataset.fy = ly; }
        group.appendChild(t);
      }
    }
  }

  // wrap a bearing delta into (-180, 180]
  function wrap180(x) { const a = ((Number(x) % 360) + 360) % 360; return a > 180 ? a - 360 : a; }
  // Ship-schematic radial vector: a fixed-up line+arrowhead inside a group rotated to `thetaDeg`
  // about the hull centre (130,180). Length in px; hidden (opacity 0) when degenerate so a
  // baseline/zero input shows nothing rather than a NaN transform.
  function setShipVec(id, thetaDeg, len) {
    const g = $(id); if (!g) return;
    if (!Number.isFinite(len) || len < 2) { g.setAttribute("opacity", "0"); return; }
    g.setAttribute("opacity", "1");
    g.setAttribute("transform", "rotate(" + Number(thetaDeg).toFixed(1) + " 130 180)");
    const line = g.querySelector("line"), head = g.querySelector("polygon");
    const tip = 180 - len;
    if (line) line.setAttribute("y2", tip.toFixed(1));
    if (head) head.setAttribute("points", "130," + tip.toFixed(1) + " 124," + (tip + 10).toFixed(1) + " 136," + (tip + 10).toFixed(1));
  }
  // Ship-schematic horizontal (athwartships) callout arrow at row y; +v = starboard (right).
  function setLatArrow(id, v, y) {
    const g = $(id); if (!g) return;
    if (!Number.isFinite(v) || Math.abs(v) < 0.05) { g.setAttribute("opacity", "0"); return; }
    g.setAttribute("opacity", "1");
    const dir = v >= 0 ? 1 : -1;
    const len = Math.max(8, Math.min(60, Math.abs(v) * 18));
    const x1 = 130 + dir * len;
    const line = g.querySelector("line"), head = g.querySelector("polygon");
    if (line) { line.setAttribute("x1", "130"); line.setAttribute("y1", String(y)); line.setAttribute("x2", x1.toFixed(1)); line.setAttribute("y2", String(y)); }
    if (head) head.setAttribute("points", x1.toFixed(1) + "," + y + " " + (x1 - dir * 10).toFixed(1) + "," + (y - 5) + " " + (x1 - dir * 10).toFixed(1) + "," + (y + 5));
  }

  // build compass rose ticks + wind rose ticks once
  let compassLabels = [];
  (function buildCompassCard() {
    const card = $("compass-card");
    if (!card) return;
    const cards = { 0: "N", 90: "E", 180: "S", 270: "W" };
    buildDialTicks(card, 100, 100, 94, 87, 80, 10, 30, {
      r: 70,
      floating: true,
      fn: (deg) => cards[deg] || ((deg < 100 ? "0" : "") + deg),
    });
    compassLabels = card.querySelectorAll("text");
  })();
  (function buildWindTicks() {
    const g = $("wind-ticks");
    if (!g) return;
    buildDialTicks(g, 100, 100, 94, 84, 84, 30, 0, null);
  })();
  // wind TRUE dial: north-up card with cardinal labels
  (function buildWtdTicks() {
    const g = $("wtd-ticks");
    if (!g) return;
    const cards = { 0: "N", 90: "E", 180: "S", 270: "W" };
    buildDialTicks(g, 80, 80, 74, 68, 60, 30, 90, { r: 50, fn: (deg) => cards[deg] || "" });
  })();
  // seed the depth-alert display constant (must match ALERT_DEPTH_M)
  (function setDepthAlert() {
    const n = $("depth-alert");
    if (n) n.textContent = ALERT_DEPTH_M.toFixed(1);
  })();
  // COG tape: a full 0-360 tick ring (5deg minor / 30deg major, 3-digit labels) built once about
  // the off-screen centre (200,460); repaint rotates it by -cog so no north-wrap seam appears.
  (function buildCogTape() {
    const g = $("cog-tape");
    if (!g) return;
    buildDialTicks(g, 200, 460, 400, 390, 380, 5, 30, {
      r: 366,
      fn: (deg) => (deg < 10 ? "00" : deg < 100 ? "0" : "") + deg,
    });
  })();
  // rudder fan scale: base arc + red hard-over zones (beyond +/-30) + ticks/labels, hung below
  // pivot (100,18); arcs drawn as sampled polylines so the geometry is flag-independent.
  (function buildRudderScale() {
    const g = $("rud-scale");
    if (!g) return;
    const NS = "http://www.w3.org/2000/svg";
    const px = 100, py = 18;
    const pt = (a, r) => [px + r * Math.sin(a * Math.PI / 180), py + r * Math.cos(a * Math.PI / 180)];
    const poly = (a0, a1, r, color, w) => {
      let d = "";
      for (let a = a0; a <= a1 + 0.001; a += 3) { const p = pt(a, r); d += (d ? " " : "") + p[0].toFixed(1) + "," + p[1].toFixed(1); }
      const e = document.createElementNS(NS, "polyline");
      e.setAttribute("points", d); e.setAttribute("fill", "none");
      e.setAttribute("stroke", color); e.setAttribute("stroke-width", w);
      g.appendChild(e);
    };
    poly(-45, 45, 92, "#263140", "1.5");
    poly(-45, -30, 92, "#ff4d4d", "3");
    poly(30, 45, 92, "#ff4d4d", "3");
    for (let a = -45; a <= 45; a += 15) {
      const p1 = pt(a, 78), p2 = pt(a, 92);
      const ln = document.createElementNS(NS, "line");
      ln.setAttribute("x1", p1[0].toFixed(1)); ln.setAttribute("y1", p1[1].toFixed(1));
      ln.setAttribute("x2", p2[0].toFixed(1)); ln.setAttribute("y2", p2[1].toFixed(1));
      ln.setAttribute("stroke", Math.abs(a) >= 30 ? "#ff4d4d" : "#7d8895");
      ln.setAttribute("stroke-width", a === 0 ? "2" : "1.2");
      g.appendChild(ln);
      const lp = pt(a, 66);
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", lp[0].toFixed(1)); t.setAttribute("y", (lp[1] + 3).toFixed(1));
      t.setAttribute("fill", "#7d8895"); t.setAttribute("font-size", "9");
      t.setAttribute("font-family", "monospace"); t.setAttribute("text-anchor", "middle");
      t.textContent = String(Math.abs(a));
      g.appendChild(t);
    }
  })();
  // inclinometer protractor scales (-30..30, 10deg ticks) drawn once above each glyph centre (80,66).
  (function buildInclScales() {
    const NS = "http://www.w3.org/2000/svg";
    const cx = 80, cy = 66;
    const pt = (a, r) => [cx + r * Math.sin(a * Math.PI / 180), cy - r * Math.cos(a * Math.PI / 180)];
    ["incl-pitch-scale", "incl-roll-scale"].forEach((gid) => {
      const g = $(gid);
      if (!g) return;
      let d = "";
      for (let a = -30; a <= 30.001; a += 3) { const p = pt(a, 44); d += (d ? " " : "") + p[0].toFixed(1) + "," + p[1].toFixed(1); }
      const arc = document.createElementNS(NS, "polyline");
      arc.setAttribute("points", d); arc.setAttribute("fill", "none");
      arc.setAttribute("stroke", "#263140"); arc.setAttribute("stroke-width", "1.5");
      g.appendChild(arc);
      for (let a = -30; a <= 30; a += 10) {
        const p1 = pt(a, 40), p2 = pt(a, 48);
        const ln = document.createElementNS(NS, "line");
        ln.setAttribute("x1", p1[0].toFixed(1)); ln.setAttribute("y1", p1[1].toFixed(1));
        ln.setAttribute("x2", p2[0].toFixed(1)); ln.setAttribute("y2", p2[1].toFixed(1));
        ln.setAttribute("stroke", a === 0 ? "#dde4ea" : "#7d8895");
        ln.setAttribute("stroke-width", a === 0 ? "1.6" : "1");
        g.appendChild(ln);
      }
    });
  })();

  // Twin-engine vertical bar gauge: an RPM track (0-3500) + a LOAD track (0-100) drawn once into
  // the 110x230 svg; each carries a bottom-anchored amber fill rect (y=BOT-h; height=h) whose id is
  // "<id>-rpm-bar" / "<id>-load-bar", updated by setEngineBar in the hot repaint. Amber = display-only.
  const ENG_TOP = 10, ENG_BOT = 200, ENG_H = ENG_BOT - ENG_TOP; // track window 190px tall
  function buildEngineGauge(id) {
    const svg = $(id);
    if (!svg) return;
    const NS = "http://www.w3.org/2000/svg";
    const TW = 30;
    const tracks = [
      { kind: "rpm", x: 16, max: 3500, ticks: [0, 875, 1750, 2625, 3500], label: "RPM" },
      { kind: "load", x: 64, max: 100, ticks: [0, 25, 50, 75, 100], label: "LOAD" },
    ];
    for (const t of tracks) {
      const bg = document.createElementNS(NS, "rect");
      bg.setAttribute("x", String(t.x)); bg.setAttribute("y", String(ENG_TOP));
      bg.setAttribute("width", String(TW)); bg.setAttribute("height", String(ENG_H));
      bg.setAttribute("rx", "3"); bg.setAttribute("fill", "#060a0f"); bg.setAttribute("stroke", "#263140");
      svg.appendChild(bg);
      for (const tv of t.ticks) {
        const y = ENG_BOT - (tv / t.max) * ENG_H;
        const ln = document.createElementNS(NS, "line");
        ln.setAttribute("x1", String(t.x - 4)); ln.setAttribute("y1", y.toFixed(1));
        ln.setAttribute("x2", String(t.x)); ln.setAttribute("y2", y.toFixed(1));
        ln.setAttribute("stroke", "#7d8895"); ln.setAttribute("stroke-width", "1");
        svg.appendChild(ln);
        const tx = document.createElementNS(NS, "text");
        tx.setAttribute("x", String(t.x - 6)); tx.setAttribute("y", (y + 3).toFixed(1));
        tx.setAttribute("fill", "#7d8895"); tx.setAttribute("font-size", "7");
        tx.setAttribute("font-family", "monospace"); tx.setAttribute("text-anchor", "end");
        tx.textContent = String(tv);
        svg.appendChild(tx);
      }
      const bar = document.createElementNS(NS, "rect");
      bar.setAttribute("id", id + "-" + t.kind + "-bar");
      bar.setAttribute("x", String(t.x + 1)); bar.setAttribute("y", String(ENG_BOT));
      bar.setAttribute("width", String(TW - 2)); bar.setAttribute("height", "0");
      bar.setAttribute("fill", "url(#engFill)");
      svg.appendChild(bar);
      const lbl = document.createElementNS(NS, "text");
      lbl.setAttribute("x", String(t.x + TW / 2)); lbl.setAttribute("y", String(ENG_BOT + 16));
      lbl.setAttribute("fill", "#7d8895"); lbl.setAttribute("font-size", "9");
      lbl.setAttribute("font-family", "monospace"); lbl.setAttribute("text-anchor", "middle");
      lbl.textContent = t.label;
      svg.appendChild(lbl);
    }
  }
  function setEngineBar(id, v, max) {
    const bar = $(id);
    if (!bar) return;
    const frac = Number.isFinite(Number(v)) ? Math.max(0, Math.min(1, Number(v) / max)) : 0;
    const h = frac * ENG_H;
    bar.setAttribute("y", (ENG_BOT - h).toFixed(1));
    bar.setAttribute("height", h.toFixed(1));
  }
  // Autopilot linear deviation indicator: 220x44 with a centre-zero baseline, ±half labels and a
  // diamond marker "<id>-mark" placed at x=110+clamp(v/half,-1,1)*100 by setLinearMarker. Amber.
  function buildLinearIndicator(id, halfRange, unit) {
    const svg = $(id);
    if (!svg) return;
    const NS = "http://www.w3.org/2000/svg";
    const cx = 110, cy = 22, x0 = 10, x1 = 210;
    const base = document.createElementNS(NS, "line");
    base.setAttribute("x1", String(x0)); base.setAttribute("y1", String(cy));
    base.setAttribute("x2", String(x1)); base.setAttribute("y2", String(cy));
    base.setAttribute("stroke", "#263140"); base.setAttribute("stroke-width", "2");
    svg.appendChild(base);
    [-1, -0.5, 0, 0.5, 1].forEach((f) => {
      const x = cx + f * 100, big = f === 0;
      const ln = document.createElementNS(NS, "line");
      ln.setAttribute("x1", x.toFixed(1)); ln.setAttribute("y1", String(cy - (big ? 8 : 5)));
      ln.setAttribute("x2", x.toFixed(1)); ln.setAttribute("y2", String(cy + (big ? 8 : 5)));
      ln.setAttribute("stroke", big ? "#7d8895" : "#1a2330"); ln.setAttribute("stroke-width", big ? "1.5" : "1");
      svg.appendChild(ln);
    });
    const mklabel = (x, txt, anchor) => {
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", String(x)); t.setAttribute("y", String(cy + 18));
      t.setAttribute("fill", "#7d8895"); t.setAttribute("font-size", "8");
      t.setAttribute("font-family", "monospace"); t.setAttribute("text-anchor", anchor);
      t.textContent = txt; svg.appendChild(t);
    };
    mklabel(x0, "-" + halfRange + unit, "start");
    mklabel(cx, "0", "middle");
    mklabel(x1, "+" + halfRange + unit, "end");
    const mk = document.createElementNS(NS, "polygon");
    mk.setAttribute("id", id + "-mark");
    mk.setAttribute("points", cx + "," + (cy - 7) + " " + (cx + 6) + "," + cy + " " + cx + "," + (cy + 7) + " " + (cx - 6) + "," + cy);
    mk.setAttribute("fill", "#00e07a");
    svg.appendChild(mk);
  }
  function setLinearMarker(id, v, half) {
    const mk = $(id);
    if (!mk) return;
    const cx = 110, cy = 22;
    const frac = Number.isFinite(Number(v)) ? Math.max(-1, Math.min(1, Number(v) / half)) : 0;
    const x = cx + frac * 100;
    mk.setAttribute("points", x.toFixed(1) + "," + (cy - 7) + " " + (x + 6).toFixed(1) + "," + cy + " " + x.toFixed(1) + "," + (cy + 7) + " " + (x - 6).toFixed(1) + "," + cy);
  }
  // build the twin engine bars + the two autopilot linear indicators once (each guards a missing target)
  (function buildPropulsionGauges() {
    buildEngineGauge("prop-port");
    buildEngineGauge("prop-stbd");
  })();
  (function buildApIndicators() {
    buildLinearIndicator("ap-offcourse", 5, "°");
    buildLinearIndicator("ap-xtd", 50, "m");
  })();
  // cosmetic Silence toggle for the alerts panel (no audio in this app; re-renders row styling)
  (function wireAlertSilence() {
    const b = $("alerts-silence");
    if (!b) return;
    b.addEventListener("click", () => {
      alertsSilenced = !alertsSilenced;
      b.textContent = alertsSilenced ? "Silenced" : "Silence";
      b.classList.toggle("active", alertsSilenced);
      renderAlerts();
    });
  })();

  function repaintConning() {
    statePending = false;
    const s = lastState;
    if (!s) return;
    const sim = s.sim || {};
    const setTxt = (id, v) => { const n = $(id); if (n) n.textContent = v; };

    // compass
    const hdg = Number(s.heading_true_deg);
    if (Number.isFinite(hdg)) {
      const compassCard = $("compass-card");
      if (compassCard) compassCard.setAttribute("transform", "rotate(" + (-hdg) + " 100 100)");
      // counter-rotate each numeral by +hdg about its own anchor so it rides the card to its
      // bearing but stays upright/readable at every heading (card rotates -hdg → net glyph 0).
      const hr = hdg.toFixed(1);
      for (let i = 0; i < compassLabels.length; i++) {
        const lbl = compassLabels[i];
        lbl.setAttribute("transform", "rotate(" + hr + " " + lbl.dataset.fx + " " + lbl.dataset.fy + ")");
      }
      const cmpHdg = $("cmp-hdg");
      if (cmpHdg) cmpHdg.textContent = num(hdg, 0);
    }
    const cog = Number(s.cog_deg);
    if (Number.isFinite(cog) && Number.isFinite(hdg)) {
      const cogMarker = $("cog-marker");
      if (cogMarker) cogMarker.setAttribute("transform", "rotate(" + (cog - hdg) + " 100 100)");
      const cmpCog = $("cmp-cog");
      if (cmpCog) cmpCog.textContent = num(cog, 0);
    }
    // COG tape ring rotates by -cog behind the fixed window (needs cog only; north-wrap free)
    const cogTape = $("cog-tape");
    if (cogTape && Number.isFinite(cog)) cogTape.setAttribute("transform", "rotate(" + (-cog).toFixed(1) + " 200 460)");

    // rate of turn (full scale +/-30 dpm -> +/-90px)
    const rot = Number(s.rot_dpm) || 0;
    const scaled = Math.max(-30, Math.min(30, rot)) / 30 * 90;
    const bar = $("rot-bar");
    if (bar) {
      if (scaled >= 0) { bar.setAttribute("x", "100"); bar.setAttribute("width", scaled.toFixed(1)); }
      else { bar.setAttribute("x", (100 + scaled).toFixed(1)); bar.setAttribute("width", (-scaled).toFixed(1)); }
    }
    const rotVal = $("rot-val");
    if (rotVal) rotVal.textContent = num(rot, 1);

    // inclinometers: rotating ship glyphs (side-view pitch, stern-view roll)
    const roll = Number(s.roll_deg) || 0, pitch = Number(s.pitch_deg) || 0;
    const inclPitchGlyph = $("incl-pitch-glyph");
    if (inclPitchGlyph) inclPitchGlyph.setAttribute("transform", "rotate(" + (-pitch).toFixed(1) + " 80 66)");
    const inclRollGlyph = $("incl-roll-glyph");
    if (inclRollGlyph) inclRollGlyph.setAttribute("transform", "rotate(" + roll.toFixed(1) + " 80 66)");
    const inclRoll = $("incl-roll");
    if (inclRoll) inclRoll.textContent = num(roll, 1);
    const inclPitch = $("incl-pitch");
    if (inclPitch) inclPitch.textContent = num(pitch, 1);

    // rudder-angle fan (green = NMEA-backed)
    const rud = Number(s.rudder_angle_deg);
    const rudNeedle = $("rud-needle");
    // Needle hangs BELOW the pivot, so a positive (starboard) angle must rotate the tip to screen
    // RIGHT where the STBD ticks are — negate to match the scale (+rud = starboard = right).
    if (rudNeedle && Number.isFinite(rud)) rudNeedle.setAttribute("transform", "rotate(" + (-rud).toFixed(1) + " 100 18)");
    setTxt("rud-val", num(s.rudder_angle_deg, 1));

    // ship schematic — every vector derived from real emitted fields (green)
    const shipCog = Number(s.cog_deg), shipHdg = Number(s.heading_true_deg);
    const shipSog = Number(s.sog_kn) || 0;
    const setDeg = Number(s.set_deg), driftKn = Number(s.drift_kn) || 0;
    const trackOk = Number.isFinite(shipCog) && Number.isFinite(shipHdg);
    const curOk = Number.isFinite(setDeg) && Number.isFinite(shipHdg);
    setShipVec("ship-vec-cog", trackOk ? shipCog - shipHdg : 0, trackOk ? Math.min(90, shipSog * 9) : 0);
    setShipVec("ship-vec-cur", curOk ? setDeg - shipHdg : 0, curOk ? Math.min(70, driftKn * 18) : 0);
    // athwartships (docking) lateral speeds recovered from ground track + yaw; L=30 m, midships pivot
    const dlt = trackOk ? wrap180(shipCog - shipHdg) : 0;
    const dRad = dlt * Math.PI / 180;
    const vLat = trackOk ? shipSog * Math.sin(dRad) : 0;
    const vFwd = trackOk ? shipSog * Math.cos(dRad) : 0;
    const tang = rot * Math.PI / (180 * 60) * 15 * 1.9438; // yaw contribution at L/2, m/s -> kn
    const vBow = vLat + tang, vStern = vLat - tang;
    setLatArrow("ship-abow", vBow, 88);
    setLatArrow("ship-astern", vStern, 280);
    setTxt("ship-vbow", Math.abs(vBow) < 0.05 ? "" : Math.abs(vBow).toFixed(2) + (vBow >= 0 ? " S" : " P"));
    setTxt("ship-vstern", Math.abs(vStern) < 0.05 ? "" : Math.abs(vStern).toFixed(2) + (vStern >= 0 ? " S" : " P"));
    setTxt("ship-vfwd", num(vFwd, 1));

    // fuel (amber = display-only, from s.sim)
    setTxt("fuel-total", num(sim.fuel_total_l, 0));
    setTxt("fuel-rate", num(sim.fuel_rate_lph, 1));
    setTxt("fuel-pernm", sim.fuel_per_nm_l == null ? "---" : num(sim.fuel_per_nm_l, 2));

    // wind — relative (apparent) dial: solid vector rotates about the 200x200 rose centre
    const appAng = Number(s.app_wind_angle_deg);
    const windApp = $("wind-app");
    if (windApp && Number.isFinite(appAng)) windApp.setAttribute("transform", "rotate(" + appAng + " 100 100)");
    // wind — true dial: north-up needle rotates to the compass wind direction (160x160 centre)
    const windDir = Number(s.wind_dir_deg);
    const wtdNeedle = $("wtd-needle");
    if (wtdNeedle && Number.isFinite(windDir)) wtdNeedle.setAttribute("transform", "rotate(" + windDir + " 80 80)");
    // wind readouts (green = NMEA-backed)
    setTxt("wrd-spd", num(s.app_wind_speed_kn, 1));
    setTxt("wrd-ang", num(s.app_wind_angle_deg, 0));
    setTxt("wtd-spd", num(s.wind_speed_kn, 1));
    setTxt("wtd-dir", num(s.wind_dir_deg, 0));

    // environment (amber = display-only, from s.sim)
    setTxt("env-wtemp", num(sim.water_temp_c, 1));
    setTxt("env-atemp", num(sim.air_temp_c, 1));
    setTxt("env-hum", num(sim.humidity_pct, 0));
    setTxt("env-press", num(sim.pressure_hpa, 0));

    // propulsion — twin engine bars (amber = display-only, from s.sim)
    setEngineBar("prop-port-rpm-bar", sim.rpm_port, 3500);
    setEngineBar("prop-port-load-bar", sim.load_port_pct, 100);
    setEngineBar("prop-stbd-rpm-bar", sim.rpm_stbd, 3500);
    setEngineBar("prop-stbd-load-bar", sim.load_stbd_pct, 100);
    setTxt("prop-port-rpm", num(sim.rpm_port, 0));
    setTxt("prop-port-load", num(sim.load_port_pct, 0));
    setTxt("prop-stbd-rpm", num(sim.rpm_stbd, 0));
    setTxt("prop-stbd-load", num(sim.load_stbd_pct, 0));

    // autopilot — mode pill + synthetic track point + metrics + linear deviation (amber = display-only)
    setTxt("ap-mode", sim.ap_mode == null ? "---" : String(sim.ap_mode));
    setTxt("ap-track-lat", fmtLat(sim.ap_track_lat));
    setTxt("ap-track-lon", fmtLon(sim.ap_track_lon));
    setTxt("ap-dist", num(sim.ap_distance_nm, 1));
    setTxt("ap-course", num(sim.ap_track_course_deg, 0));
    setTxt("ap-ttg", fmtTtg(sim.ap_time_to_go_s));
    setLinearMarker("ap-offcourse-mark", sim.ap_off_course_deg, 5);
    setLinearMarker("ap-xtd-mark", sim.ap_xtd_m, 50);

    // re-homed nav bignums (former #readouts values now live in panels; colour class is
    // assigned once in the markup, hot repaint only sets textContent)
    setTxt("pri-sog", num(s.sog_kn, 1));
    setTxt("pri-stw", num(s.stw_kn, 1));
    setTxt("pri-cog", num(s.cog_deg, 0));
    setTxt("ro-hdg", num(s.heading_true_deg, 0) + " / " + num(s.heading_mag_deg, 0));
    setTxt("ro-lat", fmtLat(s.lat));
    setTxt("ro-lon", fmtLon(s.lon));
    setTxt("ro-depth", num(s.depth_m, 1));
    setTxt("ro-utc", fmtUtc(s.utc));
    setTxt("ro-sea", num(s.sea_state, 0));

    // gauge header source tags
    const h = parseSource(channelSourceByRole("heading"));
    const cmpSrc = $("cmp-src");
    if (cmpSrc) { cmpSrc.textContent = h.tag; cmpSrc.className = "src " + h.cls; }
    const rotSrc = $("rot-src");
    if (rotSrc) { rotSrc.textContent = h.tag; rotSrc.className = "src " + h.cls; }
  }

  function requestConningPaint() {
    if (statePending) return;
    statePending = true;
    requestAnimationFrame(repaintConning);
  }

  // Depth history graph — sampled at 1 Hz off the state stream (decimated) and redrawn on
  // each sample, independent of the rAF conning repaint. Array caps at DEPTH_CAP (3 min).
  function sampleDepth(s) {
    if (!s) return;
    const d = Number(s.depth_m);
    if (!Number.isFinite(d)) return;
    const now = Date.now();
    if (now - lastDepthSampleTs < 1000) return;
    lastDepthSampleTs = now;
    depthHistory.push(d);
    while (depthHistory.length > DEPTH_CAP) depthHistory.shift();
    renderDepthGraph();
    // shallow-water flag feeds the alerts panel (rendered in applyHealth); re-render for prompt display
    const wasLow = alertFlags.depthLow;
    alertFlags.depthLow = d > 0 && d < ALERT_DEPTH_M;
    if (alertFlags.depthLow !== wasLow) renderAlerts();
  }
  function renderDepthGraph() {
    const dyn = $("depth-dyn");
    if (!dyn) return;
    const NS = "http://www.w3.org/2000/svg";
    while (dyn.firstChild) dyn.removeChild(dyn.firstChild);
    const x0 = 34, y0 = 8, x1 = 312, y1 = 100, w = x1 - x0, h = y1 - y0;
    const n = depthHistory.length;
    const mk = (tag) => document.createElementNS(NS, tag);
    const label = (x, y, txt, anchor) => {
      const t = mk("text");
      t.setAttribute("x", String(x)); t.setAttribute("y", String(y));
      t.setAttribute("fill", "#7d8895"); t.setAttribute("font-size", "9");
      t.setAttribute("font-family", "monospace"); t.setAttribute("text-anchor", anchor || "end");
      t.textContent = txt; dyn.appendChild(t);
    };
    if (n < 2) { label((x0 + x1) / 2, (y0 + y1) / 2, "acquiring depth…", "middle"); return; }
    // autoscale with 10% padding and a 2 m minimum span
    let mn = Math.min.apply(null, depthHistory), mx = Math.max.apply(null, depthHistory);
    if (mx - mn < 2) { const mid = (mn + mx) / 2; mn = mid - 1; mx = mid + 1; }
    const pad = (mx - mn) * 0.1; mn -= pad; mx += pad;
    const span = (mx - mn) || 1;
    const xstep = w / (DEPTH_CAP - 1);
    const xOf = (j) => x1 - (n - 1 - j) * xstep;      // newest at right
    const yOf = (depth) => y0 + (depth - mn) / span * h; // inverted: deeper = lower
    let pts = "";
    for (let j = 0; j < n; j++) pts += (j ? " " : "") + xOf(j).toFixed(1) + "," + yOf(depthHistory[j]).toFixed(1);
    // seabed fill below the trace
    const fill = mk("polygon");
    fill.setAttribute("points", pts + " " + xOf(n - 1).toFixed(1) + "," + y1 + " " + xOf(0).toFixed(1) + "," + y1);
    fill.setAttribute("fill", "#33280f"); fill.setAttribute("opacity", "0.55");
    dyn.appendChild(fill);
    // depth trace
    const line = mk("polyline");
    line.setAttribute("id", "depth-line");
    line.setAttribute("points", pts);
    line.setAttribute("fill", "none"); line.setAttribute("stroke", "#3fa7ff"); line.setAttribute("stroke-width", "1.5");
    dyn.appendChild(line);
    // alert threshold hline (red dashed) when within the scaled window
    if (ALERT_DEPTH_M >= mn && ALERT_DEPTH_M <= mx) {
      const al = mk("line");
      al.setAttribute("x1", String(x0)); al.setAttribute("x2", String(x1));
      al.setAttribute("y1", yOf(ALERT_DEPTH_M).toFixed(1)); al.setAttribute("y2", yOf(ALERT_DEPTH_M).toFixed(1));
      al.setAttribute("stroke", "#ff4d4d"); al.setAttribute("stroke-width", "1"); al.setAttribute("stroke-dasharray", "4 3");
      dyn.appendChild(al);
    }
    // ship marker at "Now"
    const shipDot = mk("circle");
    shipDot.setAttribute("cx", xOf(n - 1).toFixed(1)); shipDot.setAttribute("cy", yOf(depthHistory[n - 1]).toFixed(1));
    shipDot.setAttribute("r", "3"); shipDot.setAttribute("fill", "#dde4ea");
    dyn.appendChild(shipDot);
    // axis labels: shallow (top) / deep (bottom) + time span
    label(x0 - 3, y0 + 8, mn.toFixed(1));
    label(x0 - 3, y1, mx.toFixed(1));
    label(x0, y1 + 14, "-3m", "start");
    label(x1, y1 + 14, "now", "end");
  }

  // stale detector
  setInterval(() => {
    const stale = lastStateTs && (Date.now() - lastStateTs > STALE_MS);
    $("view-conning").classList.toggle("stale", !!stale);
  }, 1000);

  function updateSourceStrip() {
    const chips = $("src-chips");
    if (chips) {
      chips.textContent = "";
      const bySource = {};
      if (lastHealth && Array.isArray(lastHealth.channels)) {
        for (const c of lastHealth.channels) bySource[c.channel_id] = c.source;
      }
      for (const id of channelOrder) {
        const chip = el("span", "chip");
        chip.appendChild(el("span", "cid", id));
        const src = parseSource(bySource[id]);
        const tag = el("span", "src " + src.cls, src.tag);
        chip.appendChild(tag);
        chips.appendChild(chip);
      }
    }
    const stripMode = $("strip-mode");
    if (stripMode) stripMode.textContent = (lastHealth && lastHealth.mode) || (cfg && cfg.mode) || "—";
    const stripTime = $("strip-time");
    if (stripTime) stripTime.textContent = (lastHealth && lastHealth.time_source) ? String(lastHealth.time_source).toUpperCase() : "—";
  }

  /* =====================================================================
   *  STREAMS (reused pane grid + nmea + health), extended with toggle + source badge
   * ===================================================================== */
  function buildPane(ch, tcpHost) {
    const role = String(ch.role || ch.id || "").toLowerCase();
    const pane = el("div", "pane");

    const hdr = el("div", "pane-hdr");
    const roleEl = el("div", "role");
    roleEl.appendChild(el("span", null, ch.id));
    if (ch.talker) roleEl.appendChild(el("span", "talker", "  " + ch.talker));
    hdr.appendChild(roleEl);
    hdr.appendChild(el("span", "badge " + role, role));
    const srcBadge = el("span", "src src-off", "OFF");
    hdr.appendChild(srcBadge);
    pane.appendChild(hdr);

    const stats = el("div", "stats");
    const sAlive = el("span", "stat-alive", "…");
    const sEmit = document.createElement("span"); sEmit.innerHTML = 'emitted <b>0</b>';
    const sEmitVal = sEmit.querySelector("b");
    const sErr = document.createElement("span"); sErr.innerHTML = 'build err <b>0</b>';
    const sErrVal = sErr.querySelector("b");
    const sSinks = el("span", "sinks");
    stats.appendChild(sAlive); stats.appendChild(sEmit); stats.appendChild(sErr); stats.appendChild(sSinks);
    pane.appendChild(stats);

    const tap = ch.tcp_tap;
    if (tap && tap.enabled && tap.port) {
      const conn = (tcpHost || "127.0.0.1") + ":" + tap.port;
      const tcp = el("div", "tcp");
      tcp.appendChild(el("span", "label", "TCP tap"));
      const code = el("code", null, conn);
      tcp.appendChild(code);
      const btn = el("button", "small", "Copy");
      btn.addEventListener("click", () => copyText(conn));
      tcp.appendChild(btn);
      tcp.appendChild(el("span", "note", "(raw TCP, not a URL)"));
      pane.appendChild(tcp);
    }

    const feed = el("div", "feed");
    pane.appendChild(feed);

    const ctl = el("div", "pane-ctl");
    // real output toggle
    const swWrap = el("label", "switch");
    const sw = document.createElement("input"); sw.type = "checkbox"; sw.checked = ch.enabled !== false;
    swWrap.appendChild(sw); swWrap.appendChild(document.createTextNode("Output"));
    const frozenTag = el("span", "frozen-tag", "");
    const btnFreeze = el("button", "small", "Freeze view");
    const btnClear = el("button", "small", "Clear");
    ctl.appendChild(swWrap);
    ctl.appendChild(btnFreeze);
    ctl.appendChild(btnClear);
    ctl.appendChild(frozenTag);
    pane.appendChild(ctl);

    const state = {
      pane, feed, srcBadge, sw, frozen: false, count: 0, enabled: ch.enabled !== false,
      sAlive, sEmitVal, sErrVal, sSinks, frozenTag, btnFreeze,
    };

    btnFreeze.addEventListener("click", () => {
      state.frozen = !state.frozen;
      btnFreeze.textContent = state.frozen ? "Resume view" : "Freeze view";
      feed.classList.toggle("frozen", state.frozen);
      frozenTag.textContent = state.frozen ? "VIEW FROZEN (log paused)" : "";
    });
    btnClear.addEventListener("click", () => { feed.textContent = ""; state.count = 0; });
    sw.addEventListener("change", async () => {
      try { await control({ action: "channel", channel_id: ch.id, enabled: sw.checked }); }
      catch (e) { toast("Toggle failed: " + e.message); sw.checked = !sw.checked; }
    });

    panes[ch.id] = state;
    $("panes").appendChild(pane);
  }

  // The consolidated pane: a live mirror of the aggregate TCP tap — every channel's sentences
  // merged into one feed, exactly what a client connecting to the aggregate port would receive.
  function buildAggregatePane(port, tcpHost) {
    const pane = el("div", "pane");

    const hdr = el("div", "pane-hdr");
    const roleEl = el("div", "role");
    roleEl.appendChild(el("span", null, "Consolidated"));
    hdr.appendChild(roleEl);
    hdr.appendChild(el("span", "badge tcp-tap", "all channels"));
    pane.appendChild(hdr);

    const conn = (tcpHost || "127.0.0.1") + ":" + port;
    const tcp = el("div", "tcp");
    tcp.appendChild(el("span", "label", "TCP tap"));
    tcp.appendChild(el("code", null, conn));
    const btn = el("button", "small", "Copy");
    btn.addEventListener("click", () => copyText(conn));
    tcp.appendChild(btn);
    tcp.appendChild(el("span", "note", "(raw TCP, not a URL)"));
    pane.appendChild(tcp);

    const feed = el("div", "feed");
    pane.appendChild(feed);

    const ctl = el("div", "pane-ctl");
    const frozenTag = el("span", "frozen-tag", "");
    const btnFreeze = el("button", "small", "Freeze view");
    const btnClear = el("button", "small", "Clear");
    ctl.appendChild(btnFreeze);
    ctl.appendChild(btnClear);
    ctl.appendChild(frozenTag);
    pane.appendChild(ctl);

    const state = { pane, feed, frozen: false, count: 0 };
    btnFreeze.addEventListener("click", () => {
      state.frozen = !state.frozen;
      btnFreeze.textContent = state.frozen ? "Resume view" : "Freeze view";
      feed.classList.toggle("frozen", state.frozen);
      frozenTag.textContent = state.frozen ? "VIEW FROZEN (log paused)" : "";
    });
    btnClear.addEventListener("click", () => { feed.textContent = ""; state.count = 0; });

    aggState = state;
    $("panes").appendChild(pane);
  }

  function pushLine(p, line) {
    if (!p || p.frozen) return;
    const div = el("div", "line");
    div.appendChild(document.createTextNode(line));
    const crlf = el("span", "crlf", "␍␊");
    crlf.title = "\\r\\n (CR LF)";
    div.appendChild(crlf);
    p.feed.appendChild(div);
    p.count++;
    while (p.count > MAX_LINES && p.feed.firstChild) { p.feed.removeChild(p.feed.firstChild); p.count--; }
    const nearBottom = p.feed.scrollHeight - p.feed.scrollTop - p.feed.clientHeight < 40;
    if (nearBottom) p.feed.scrollTop = p.feed.scrollHeight;
  }

  function appendLine(chId, line) {
    // Route to the channel's own pane (if it has one) AND the consolidated pane, which mirrors
    // the aggregate TCP tap by showing every channel's sentences merged, live.
    pushLine(panes[chId], line);
    pushLine(aggState, line);
  }

  // Derive the active alert set from the last health frame + state-frame flags. Each alert carries a
  // stable `key` so an acknowledge sticks to a condition and auto-clears (re-arms) when it disappears.
  function deriveAlerts() {
    const out = [];
    const h = lastHealth;
    if (h && typeof h === "object") {
      if (h.status === "stopped") out.push({ key: "engine-stopped", sev: "crit", text: "Engine stopped" });
      else if (h.ok === false) out.push({ key: "engine-degraded", sev: "warn", text: "Engine degraded" });
      const chans = Array.isArray(h.channels) ? h.channels : [];
      for (const c of chans) {
        const id = c.channel_id || c.id;
        const enabled = c.enabled !== false;
        if (enabled && c.alive === false) out.push({ key: "dead:" + id, sev: "crit", text: id + " channel dead" });
        if (typeof c.build_errors === "number" && c.build_errors > 0) out.push({ key: "errs:" + id, sev: "warn", text: id + " build errors (" + c.build_errors + ")" });
        const sinks = Array.isArray(c.sinks) ? c.sinks : [];
        for (const sk of sinks) {
          if (sk.down === true) out.push({ key: "sink:" + id + ":" + sk.name, sev: "warn", text: id + " sink down: " + sk.name });
        }
      }
    }
    if (alertFlags.depthLow) out.push({ key: "depth-low", sev: "crit", text: "Shallow water < " + ALERT_DEPTH_M.toFixed(1) + " m" });
    return out;
  }
  // Render the alerts panel. Called from applyHealth (so rows keep refreshing when state frames stop)
  // and from the depth sampler (so a shallow reading shows promptly). NOT from repaintConning.
  function renderAlerts() {
    const list = $("alerts-list");
    if (!list) return;
    const alerts = deriveAlerts();
    const now = Date.now();
    const activeKeys = new Set(alerts.map((a) => a.key));
    // drop age + acknowledge state for cleared conditions so a reappearance re-alarms + resets the age
    for (const k of Object.keys(alertFirstSeen)) if (!activeKeys.has(k)) delete alertFirstSeen[k];
    for (const k of Array.from(ackedAlerts)) if (!activeKeys.has(k)) ackedAlerts.delete(k);
    list.textContent = "";
    if (!alerts.length) { list.appendChild(el("div", "hint", "No active alerts")); return; }
    alerts.sort((a, b) => (a.sev === "crit" ? 0 : 1) - (b.sev === "crit" ? 0 : 1));
    for (const a of alerts) {
      if (!(a.key in alertFirstSeen)) alertFirstSeen[a.key] = now;
      const acked = ackedAlerts.has(a.key);
      const row = el("div", "alert-row" + (acked ? " acked" : "") + (alertsSilenced ? " silenced" : ""));
      row.appendChild(el("span", "alert-dot " + (a.sev === "crit" ? "crit" : "warn")));
      row.appendChild(el("span", "alert-text", a.text));
      row.appendChild(el("span", "alert-age", fmtAge(Math.round((now - alertFirstSeen[a.key]) / 1000))));
      const ack = el("button", "small alert-ack", acked ? "Ack'd" : "Ack");
      ack.addEventListener("click", () => {
        if (ackedAlerts.has(a.key)) ackedAlerts.delete(a.key); else ackedAlerts.add(a.key);
        renderAlerts();
      });
      row.appendChild(ack);
      list.appendChild(row);
    }
  }

  function applyHealth(h) {
    if (!h || typeof h !== "object") return;
    lastHealth = h;
    if (h.status === "stopped") {
      $("eng-dot").className = "dot dead";
      $("eng-label").textContent = "engine stopped";
    } else {
      const ok = h.ok !== false;
      $("eng-dot").className = "dot " + (ok ? "live" : "dead");
      $("eng-label").textContent = ok ? "engine ok" : "engine degraded";
    }
    const chans = Array.isArray(h.channels) ? h.channels : [];
    for (const c of chans) {
      const id = c.channel_id || c.id;
      const p = panes[id];
      if (!p) continue;
      const enabled = c.enabled !== false;
      p.enabled = enabled;
      if (p.sw.checked !== enabled) p.sw.checked = enabled;
      p.pane.classList.toggle("ch-off", !enabled);
      // staleness alarm suppressed when disabled
      const alive = enabled ? (c.alive !== false) : true;
      p.sAlive.textContent = !enabled ? "○ output off" : (alive ? "● alive" : "○ dead");
      p.sAlive.className = "stat-alive " + (!enabled ? "" : (alive ? "up" : "down"));
      if (typeof c.emitted === "number") p.sEmitVal.textContent = c.emitted.toLocaleString();
      if (typeof c.build_errors === "number") p.sErrVal.textContent = c.build_errors;
      // source badge
      const src = parseSource(c.source);
      p.srcBadge.textContent = src.tag;
      p.srcBadge.className = "src " + src.cls;
      // sinks
      p.sSinks.textContent = "";
      const sinks = Array.isArray(c.sinks) ? c.sinks : [];
      if (sinks.length === 0) p.sSinks.appendChild(el("span", "sink", "no sinks"));
      for (const s of sinks) {
        const wrap = el("span", "sink");
        const down = s.down === true;
        wrap.appendChild(el("span", "dot " + (down ? "dead" : "live")));
        wrap.appendChild(document.createTextNode(s.name + (s.errors ? " (" + s.errors + ")" : "")));
        p.sSinks.appendChild(wrap);
      }
    }
    updateSourceStrip();
    updateConfigChannelSources();
    renderAlerts();
  }

  /* =====================================================================
   *  SSE (single EventSource shared by all tabs)
   * ===================================================================== */
  let es = null;
  function connectStream() {
    es = new EventSource("/api/stream");
    es.addEventListener("open", () => { $("sse-dot").className = "dot live"; $("sse-label").textContent = "stream live"; });
    es.addEventListener("error", () => { $("sse-dot").className = "dot dead"; $("sse-label").textContent = "stream reconnecting…"; });
    es.addEventListener("nmea", (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d && d.channel != null) appendLine(d.channel, d.line != null ? d.line : "");
    });
    es.addEventListener("health", (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      applyHealth(d);
    });
    es.addEventListener("state", (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      lastState = d; lastStateTs = Date.now();
      sampleDepth(d);
      if (activeTab === "conning") requestConningPaint();
      else if (activeTab === "config") renderRouteProgress(d.route || null);
    });
  }

  /* =====================================================================
   *  CONTROL POST helper
   * ===================================================================== */
  async function control(body) {
    const res = await fetch("/api/control", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let data = null; try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      const detail = data && (data.detail || data.error || data.message);
      throw new Error(detail || ("HTTP " + res.status));
    }
    return data;
  }
  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let data = null; try { data = await res.json(); } catch (e) {}
    return { ok: res.ok, status: res.status, data };
  }

  $("btn-start").addEventListener("click", async () => {
    try { await control({ action: "start" }); toast("Engine started."); }
    catch (e) { toast("Start failed: " + e.message); }
  });
  $("btn-stop").addEventListener("click", async () => {
    try { await control({ action: "stop" }); toast("Engine stopped."); }
    catch (e) { toast("Stop failed: " + e.message); }
  });

  /* =====================================================================
   *  CONFIG TAB
   * ===================================================================== */
  const CFG_FIELDS = {
    nav: [
      ["lat", "Latitude"], ["lon", "Longitude"], ["sog_kn", "SOG (kn)"], ["cog_deg", "COG (°)"],
      ["heading_true_deg", "Heading T (°)"], ["heading_mag_deg", "Heading M (°)"], ["mag_variation_deg", "Variation (°)"],
    ],
    inst: [
      ["stw_kn", "STW (kn)"], ["depth_m", "Depth (m)"], ["rot_dpm", "ROT (°/min)"],
      ["wind_speed_kn", "Wind spd (kn)"], ["wind_dir_deg", "Wind dir (°)"],
    ],
    gps: [
      ["altitude_m", "Altitude (m)"], ["fix_quality", "Fix quality"], ["satellites", "Satellites"], ["hdop", "HDOP"],
    ],
  };
  function buildConfigForms() {
    const mk = (containerId, fields) => {
      const c = $(containerId); c.textContent = "";
      for (const [key, label] of fields) {
        const f = el("div", "field");
        f.appendChild(el("label", null, label));
        const inp = document.createElement("input");
        inp.type = "number"; inp.step = "any"; inp.id = "cfg-" + key; inp.dataset.field = key;
        f.appendChild(inp);
        c.appendChild(f);
      }
    };
    mk("grp-nav", CFG_FIELDS.nav);
    mk("grp-inst", CFG_FIELDS.inst);
    mk("grp-gps", CFG_FIELDS.gps);
    // Environment: sea-state selector
    const env = $("grp-env"); env.textContent = "";
    const f = el("div", "field");
    f.appendChild(el("label", null, "Sea state (0–9)"));
    const sel = document.createElement("select"); sel.id = "cfg-sea_state"; sel.dataset.field = "sea_state";
    sel.style.width = "200px";
    SEA_STATES.forEach((lbl, i) => { const o = document.createElement("option"); o.value = String(i); o.textContent = lbl; sel.appendChild(o); });
    f.appendChild(sel);
    env.appendChild(f);
  }

  function allCfgInputs() {
    return Array.from(document.querySelectorAll("[data-field]"));
  }
  function readCfgField(key) {
    const node = $("cfg-" + key);
    if (!node) return null;
    const raw = String(node.value).trim();
    if (raw === "") return null;
    const n = Number(raw);
    return { node, raw, n };
  }
  function validateCfgField(key) {
    const v = readCfgField(key);
    const node = $("cfg-" + key);
    if (!v) { if (node) node.classList.remove("invalid"); return { present: false }; }
    if (!Number.isFinite(v.n)) { node.classList.add("invalid"); return { present: true, ok: false, msg: key + " is not a number" }; }
    const [lo, hi] = RANGES[key] || [null, null];
    if ((lo != null && v.n < lo) || (hi != null && v.n > hi)) {
      node.classList.add("invalid");
      return { present: true, ok: false, msg: key + "=" + v.n + " out of range [" + lo + ", " + hi + "]" };
    }
    node.classList.remove("invalid");
    return { present: true, ok: true, value: v.n };
  }

  function buildConfigChannels() {
    const c = $("cfg-channels"); c.textContent = "";
    for (const id of channelOrder) {
      const meta = channelMeta[id] || {};
      const wrap = el("div", "field");
      const lab = el("label", "switch");
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.id = "cfg-ch-" + id;
      const chSpec = (cfg.channels || []).find((x) => x.id === id);
      cb.checked = chSpec ? chSpec.enabled !== false : true;
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(" " + id + " (" + (meta.role || "") + ")"));
      wrap.appendChild(lab);
      const srcTag = el("span", "src src-off", "OFF"); srcTag.id = "cfg-ch-src-" + id;
      wrap.appendChild(srcTag);
      c.appendChild(wrap);
    }
    updateConfigChannelSources();
  }
  function updateConfigChannelSources() {
    if (!lastHealth || !Array.isArray(lastHealth.channels)) return;
    for (const ch of lastHealth.channels) {
      const node = $("cfg-ch-src-" + ch.channel_id);
      if (!node) continue;
      const src = parseSource(ch.source);
      node.textContent = src.tag; node.className = "src " + src.cls;
    }
    // AIS Traffic group reuses the ais channel's per-channel source badge to show active state.
    const aisId = channelOrder.find((id) => (channelMeta[id] || {}).role === "ais");
    const aisCh = aisId ? lastHealth.channels.find((c) => c.channel_id === aisId) : null;
    const aisNode = $("cfg-ais-traffic-src");
    if (aisCh && aisNode) {
      const src = parseSource(aisCh.source);
      aisNode.textContent = src.tag; aisNode.className = "src " + src.cls;
    }
  }

  // --- F4: per-channel sentence enable/rate table (built from cfg.channels[].emit) ---
  function buildConfigSentences() {
    const wrap = $("cfg-sentences"); wrap.textContent = "";
    const channels = (cfg && Array.isArray(cfg.channels)) ? cfg.channels : [];
    let any = false;
    for (const ch of channels) {
      const emit = Array.isArray(ch.emit) ? ch.emit : [];
      if (!emit.length) continue;
      any = true;
      wrap.appendChild(el("h3", null, ch.id + " (" + (ch.role || "") + ")"));
      const tbl = el("table", "kvtable"); tbl.style.maxWidth = "420px";
      const hr = document.createElement("tr");
      hr.appendChild(el("td", null, "sentence"));
      hr.appendChild(el("td", null, "on"));
      hr.appendChild(el("td", null, "rate (Hz)"));
      tbl.appendChild(hr);
      for (const e of emit) {
        const tr = document.createElement("tr");
        tr.appendChild(el("td", null, e.sentence));
        const cbTd = document.createElement("td");
        const cb = document.createElement("input");
        cb.type = "checkbox"; cb.checked = e.enabled !== false;
        cb.dataset.sentRole = "enable"; cb.dataset.ch = ch.id; cb.dataset.sentence = e.sentence;
        cbTd.appendChild(cb); tr.appendChild(cbTd);
        const rtTd = document.createElement("td");
        const rt = document.createElement("input");
        rt.type = "number"; rt.step = "any"; rt.value = e.rate_hz;
        rt.style.width = "80px";
        rt.dataset.sentRole = "rate"; rt.dataset.ch = ch.id; rt.dataset.sentence = e.sentence;
        rtTd.appendChild(rt); tr.appendChild(rtTd);
        tbl.appendChild(tr);
      }
      wrap.appendChild(tbl);
    }
    if (!any) wrap.appendChild(el("span", "hint", "No channels with emit sentences."));
  }
  // Gather per-channel emit overrides -> {chId: [{sentence, enabled, rate_hz}]}
  function collectEmitOverrides() {
    const byCh = {};
    const ensure = (ch, sent) => {
      (byCh[ch] = byCh[ch] || {});
      return (byCh[ch][sent] = byCh[ch][sent] || { sentence: sent });
    };
    for (const cb of document.querySelectorAll("input[data-sent-role='enable']")) {
      ensure(cb.dataset.ch, cb.dataset.sentence).enabled = cb.checked;
    }
    for (const rt of document.querySelectorAll("input[data-sent-role='rate']")) {
      const raw = String(rt.value).trim(); const v = Number(raw);
      if (raw !== "" && Number.isFinite(v)) ensure(rt.dataset.ch, rt.dataset.sentence).rate_hz = v;
    }
    const out = {};
    for (const ch of Object.keys(byCh)) out[ch] = Object.values(byCh[ch]);
    return out;
  }

  // --- F1/F2: load route + replay blocks from cfg into the form ---
  function loadRouteReplayIntoConfig() {
    const r = (cfg && cfg.route) || null;
    $("cfg-route-enabled").checked = !!(r && r.enabled);
    $("cfg-route-loop").checked = !!(r && r.loop);
    $("cfg-route-speed").value = (r && r.speed_kn != null) ? r.speed_kn : "";
    $("cfg-route-wpts").value = (r && Array.isArray(r.waypoints))
      ? r.waypoints.map((w) => w[0] + ", " + w[1]).join("\n") : "";
    const rp = (cfg && cfg.replay) || null;
    $("cfg-replay-file").value = rp ? (rp.file || "") : "";
    $("cfg-replay-loop").checked = !!(rp && rp.loop);
    $("cfg-replay-speed").value = (rp && rp.speed != null) ? rp.speed : "1";
    $("cfg-replay-scope").value = (rp && rp.scope) ? rp.scope : "full";
  }
  // --- Scope B: AIS synthetic-traffic block (profiles dropdown + enable + count) ---
  function aisTrafficFromCfg() {
    const channels = (cfg && Array.isArray(cfg.channels)) ? cfg.channels : [];
    const ch = channels.find((c) => String(c.role || "").toLowerCase() === "ais");
    return (ch && ch.ais && ch.ais.traffic) || null;
  }
  function baseName(p) {
    if (!p) return "";
    const s = String(p);
    const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
    return i >= 0 ? s.slice(i + 1) : s;
  }
  async function loadProfiles(selected) {
    const sel = $("cfg-ais-profile");
    if (!sel) return;
    let names = [];
    try {
      const res = await fetch("/api/profiles");
      const d = await res.json();
      if (d && Array.isArray(d.profiles)) names = d.profiles;
    } catch (e) { /* keep the neutral-default-only dropdown */ }
    const want = selected != null ? selected : sel.value;
    sel.textContent = "";
    const dflt = document.createElement("option");
    dflt.value = ""; dflt.textContent = "Neutral default";
    sel.appendChild(dflt);
    for (const name of names) {
      const o = document.createElement("option");
      o.value = name; o.textContent = name;
      sel.appendChild(o);
    }
    sel.value = want || "";
  }
  async function loadAisTrafficIntoConfig() {
    const t = aisTrafficFromCfg();
    $("cfg-ais-enabled").checked = !!(t && t.enabled);
    $("cfg-ais-count").value = (t && t.target_count != null) ? t.target_count : "";
    await loadProfiles(t ? baseName(t.profile_path) : "");
  }

  function parseWaypoints() {
    const wpts = [];
    for (const line of String($("cfg-route-wpts").value).split(/\r?\n/)) {
      const t = line.trim();
      if (!t) continue;
      const parts = t.split(/[,\s]+/).map(Number);
      if (parts.length >= 2 && Number.isFinite(parts[0]) && Number.isFinite(parts[1])) {
        wpts.push([parts[0], parts[1]]);
      }
    }
    return wpts;
  }

  // --- F1: runtime route control + progress readout ---
  function renderRouteProgress(r) {
    const node = $("route-progress");
    if (!r) { node.textContent = "No active route."; return; }
    const pct = (Number(r.fraction) * 100 || 0).toFixed(0);
    const flag = r.finished ? "finished" : (r.paused ? "paused" : "running");
    node.textContent = "Waypoint " + r.active_waypoint + " / " + r.waypoint_count +
      " (" + pct + "%) · " + flag;
  }
  async function routeOp(op) {
    try {
      const r = await control({ action: "route", op });
      renderRouteProgress(r && r.route);
      toast("Route " + op);
    } catch (e) { $("route-progress").textContent = "Route " + op + " failed: " + e.message; }
  }
  $("route-start").addEventListener("click", () => routeOp("start"));
  $("route-pause").addEventListener("click", () => routeOp("pause"));
  $("route-reset").addEventListener("click", () => routeOp("reset"));

  // --- F3: GPS-fault injection (simulate-only testing tool) ---
  function setFaultMsg(t, c) { const m = $("fault-msg"); m.textContent = t || ""; m.className = "msg" + (c ? " " + c : ""); }
  async function faultAction(fault, valInputId) {
    const body = { action: "fault", fault };
    if (valInputId) {
      const raw = String($(valInputId).value).trim(); const v = Number(raw);
      if (raw === "" || !Number.isFinite(v)) { setFaultMsg(fault + " needs a numeric value", "err"); return; }
      body.value = v;
    }
    try { await control(body); setFaultMsg(fault + " injected", "ok"); }
    catch (e) { setFaultMsg(fault + " refused: " + e.message, "err"); }
  }
  $("fault-no_fix").addEventListener("click", () => faultAction("no_fix"));
  $("fault-restore_fix").addEventListener("click", () => faultAction("restore_fix"));
  $("fault-hdop_spike").addEventListener("click", () => faultAction("hdop_spike", "fault-hdop-val"));
  $("fault-drop_sats").addEventListener("click", () => faultAction("drop_sats", "fault-sats-val"));
  $("fault-gps_kill").addEventListener("click", () => faultAction("gps_kill"));
  $("fault-gps_restore").addEventListener("click", () => faultAction("gps_restore"));

  function setMode(mode) {
    for (const r of document.querySelectorAll('input[name="cfg-mode"]')) r.checked = (r.value === mode);
    const auto = mode === "auto";
    const sim = mode === "simulate";
    const replay = mode === "replay";
    $("cfg-inputs-card").style.opacity = auto ? "1" : "0.55";
    $("cfg-inputs-note").style.display = auto ? "none" : "block";
    $("cfg-inputs").style.display = auto ? "flex" : "none";
    // replay file/loop/speed knobs only apply in replay mode
    $("cfg-replay-fields").style.display = replay ? "flex" : "none";
    // route + fault injection are simulate-only tools
    for (const cid of ["cfg-route-card", "cfg-fault-card"]) {
      const card = $(cid);
      card.style.opacity = sim ? "1" : "0.55";
      const note = card.querySelector(".mode-note");
      if (note) note.style.display = sim ? "none" : "block";
    }
  }
  document.addEventListener("change", (ev) => {
    if (ev.target && ev.target.name === "cfg-mode") setMode(ev.target.value);
  });

  async function refreshInputs() {
    try {
      const res = await fetch("/api/inputs");
      const inputs = await res.json();
      renderConfigInputs(Array.isArray(inputs) ? inputs : []);
    } catch (e) { /* leave prior render */ }
  }
  function renderConfigInputs(inputs) {
    const c = $("cfg-inputs"); c.textContent = "";
    if (inputs.length === 0) { c.appendChild(el("span", "hint", "No provisioned input slots.")); return; }
    for (const inp of inputs) {
      const wrap = el("div", "field");
      wrap.appendChild(el("label", null, "Slot " + inp.id));
      const sel = document.createElement("select"); sel.id = "cfg-in-" + inp.id; sel.dataset.slot = inp.id;
      for (const fn of ["gps", "sat", "ais", "unused"]) {
        const o = document.createElement("option"); o.value = fn; o.textContent = fn.toUpperCase();
        if (inp.function === fn) o.selected = true;
        sel.appendChild(o);
      }
      wrap.appendChild(sel);
      const det = el("div", "hint");
      const cls = inp.detected_class ? inp.detected_class : "none";
      const live = inp.live ? "live" : "idle";
      det.textContent = "detected: " + cls + " · " + live;
      wrap.appendChild(det);
      if (inp.mismatch) {
        const w = el("div", "src src-sim", "MISMATCH");
        w.title = "declared function conflicts with detected class";
        wrap.appendChild(w);
      }
      c.appendChild(wrap);
    }
  }

  function loadStateIntoConfig(s) {
    for (const key of Object.keys(RANGES)) {
      if (key === "sea_state") continue;
      const node = $("cfg-" + key);
      if (node && s[key] != null) node.value = s[key];
    }
    const sea = $("cfg-sea_state");
    if (sea && s.sea_state != null) sea.value = String(Math.max(0, Math.min(9, Math.round(Number(s.sea_state)))));
  }

  $("cfg-apply").addEventListener("click", async () => {
    const body = { action: "update" };
    let any = false;
    for (const node of allCfgInputs()) {
      const key = node.dataset.field;
      if (key === "sea_state") continue; // handled below (it is a select, always valued)
      const v = validateCfgField(key);
      if (v.present && v.ok === false) { setCfgMsg(v.msg, "err"); return; }
      if (v.present && v.ok) { body[key] = v.value; any = true; }
    }
    // sea_state from selector
    const sea = $("cfg-sea_state");
    if (sea && sea.value !== "") { body.sea_state = Number(sea.value); any = true; }
    if (!any) { setCfgMsg("Enter at least one field to apply.", "err"); return; }
    try { await control(body); setCfgMsg("Applied to running engine.", "ok"); }
    catch (e) { setCfgMsg("Apply failed: " + e.message, "err"); }
  });

  $("cfg-save").addEventListener("click", async () => {
    const body = {};
    // allow-listed manual fields only
    for (const key of INITIAL_FIELDS) {
      if (key === "sea_state") { const sea = $("cfg-sea_state"); if (sea && sea.value !== "") body.sea_state = Number(sea.value); continue; }
      const v = validateCfgField(key);
      if (v.present && v.ok === false) { setCfgMsg(v.msg, "err"); return; }
      if (v.present && v.ok) body[key] = v.value;
    }
    // mode
    const modeR = document.querySelector('input[name="cfg-mode"]:checked');
    if (modeR) body.mode = modeR.value;
    // channels (+ F4 per-sentence emit overrides)
    const emitMap = collectEmitOverrides();
    body.channels = channelOrder.map((id) => {
      const entry = { id, enabled: $("cfg-ch-" + id).checked };
      const em = emitMap[id];
      if (em && em.length) entry.emit = em;
      return entry;
    });
    // inputs (only if any selectors present)
    const inputSels = Array.from(document.querySelectorAll("[data-slot]"));
    if (inputSels.length) body.inputs = inputSels.map((s) => ({ id: s.dataset.slot, function: s.value }));
    // F1 route block (persist when enabled or any waypoints supplied)
    const routeEnabled = $("cfg-route-enabled").checked;
    const wpts = parseWaypoints();
    if (routeEnabled || wpts.length) {
      const spRaw = String($("cfg-route-speed").value).trim();
      body.route = {
        enabled: routeEnabled,
        waypoints: wpts,
        speed_kn: spRaw === "" ? 0 : Number(spRaw),
        loop: $("cfg-route-loop").checked,
      };
    }
    // F2 replay block (persist when replay mode or a file is named)
    const replayFile = $("cfg-replay-file").value.trim();
    if (body.mode === "replay" || replayFile) {
      const spRaw = String($("cfg-replay-speed").value).trim();
      body.replay = {
        enabled: body.mode === "replay",
        file: replayFile,
        loop: $("cfg-replay-loop").checked,
        speed: spRaw === "" ? 1.0 : Number(spRaw),
        scope: $("cfg-replay-scope").value,
      };
    }
    // Scope B: AIS synthetic-traffic block (feeds the SAME allow-listed persist call). M10: only
    // send it when the config actually HAS an ais-role channel — otherwise every save 400s on a
    // no-AIS config. profile_path is the dropdown's basename and is sent ONLY for a real selection
    // (never null), so a failed /api/profiles fetch can't silently revert a saved profile to the
    // neutral default; target_count only when a number is given.
    const aisCh = (cfg && Array.isArray(cfg.channels))
      ? cfg.channels.find((c) => String(c.role || "").toLowerCase() === "ais") : null;
    if (aisCh) {
      const at = { enabled: $("cfg-ais-enabled").checked };
      const profile = $("cfg-ais-profile").value;
      if (profile) at.profile_path = profile;
      const countRaw = String($("cfg-ais-count").value).trim();
      if (countRaw !== "" && Number.isFinite(Number(countRaw))) at.target_count = Number(countRaw);
      body.ais_traffic = at;
    }
    const r = await postJson("/api/config/initial-state", body);
    if (r.ok) setCfgMsg("Saved as defaults (applies on next Start).", "ok");
    else setCfgMsg("Save failed: " + ((r.data && r.data.detail) || ("HTTP " + r.status)), "err");
  });

  $("cfg-load").addEventListener("click", loadConfigCurrent);
  async function loadConfigCurrent() {
    try {
      const [stRes, cfgRes] = await Promise.all([fetch("/api/state"), fetch("/api/config")]);
      const s = await stRes.json();
      cfg = await cfgRes.json();
      if (s && s.running !== false) loadStateIntoConfig(s);
      setMode(cfg.mode || "simulate");
      // refresh channel checkboxes from config
      for (const ch of (cfg.channels || [])) { const cb = $("cfg-ch-" + ch.id); if (cb) cb.checked = ch.enabled !== false; }
      buildConfigSentences();
      loadRouteReplayIntoConfig();
      await loadAisTrafficIntoConfig();
      await refreshInputs();
      setCfgMsg((s && s.running !== false) ? "Loaded current state + config." : "Loaded config (engine stopped — no live state).", "ok");
    } catch (e) { setCfgMsg("Load failed: " + e.message, "err"); }
  }
  function setCfgMsg(t, c) { const m = $("cfg-msg"); m.textContent = t || ""; m.className = "msg" + (c ? " " + c : ""); }

  /* =====================================================================
   *  MAINTENANCE TAB (poll /api/diag while active)
   * ===================================================================== */
  function startDiagPoll() { pollDiag(); diagTimer = setInterval(pollDiag, 2000); }
  function stopDiagPoll() { if (diagTimer) { clearInterval(diagTimer); diagTimer = null; } }
  async function pollDiag() {
    let d;
    try { const res = await fetch("/api/diag"); d = await res.json(); } catch (e) { return; }
    renderDiag(d);
  }
  function verdictClass(v) {
    v = String(v || "");
    if (v === "valid") return "good";
    if (v === "no-data" || v === "reversed-ab" || v === "wrong-baud" || v === "collision") return "bad";
    return "warnv";
  }
  function renderDiag(d) {
    const grid = $("diag-grid"); grid.textContent = "";
    const ports = (d && Array.isArray(d.ports)) ? d.ports : [];
    $("diag-empty").style.display = ports.length ? "none" : "block";
    for (const p of ports) {
      const card = el("div", "gauge"); card.style.alignItems = "stretch";
      const top = el("div", "g-title");
      top.appendChild(el("span", null, p.port_id));
      const vd = el("span", "verdict " + verdictClass(p.verdict), String(p.verdict || "?"));
      top.appendChild(vd);
      card.appendChild(top);

      card.appendChild(el("div", "hint", String(p.advice || "")));

      // checksum error rate
      const structured = (Number(p.valid) || 0) + (Number(p.bad_checksum) || 0);
      const errRate = structured ? (Number(p.bad_checksum) || 0) / structured : 0;
      const meterLbl = el("div", "hint", "checksum errors: " + (Number(p.bad_checksum) || 0) + " / " + structured + " (" + (errRate * 100).toFixed(1) + "%)");
      meterLbl.style.marginTop = "8px";
      card.appendChild(meterLbl);
      const meter = el("div", "meter");
      const bar = document.createElement("span"); bar.style.width = Math.min(100, errRate * 100).toFixed(1) + "%";
      bar.style.background = errRate > 0.2 ? "var(--down)" : (errRate > 0.02 ? "var(--warn)" : "var(--ok)");
      meter.appendChild(bar); card.appendChild(meter);

      const tbl = el("table", "kvtable"); tbl.style.marginTop = "8px";
      const row = (k, v) => { const tr = document.createElement("tr"); tr.appendChild(el("td", null, k)); tr.appendChild(el("td", null, v)); tbl.appendChild(tr); };
      row("sentences/s", num(p.sentences_per_s, 2));
      row("bus load", num(p.bus_load_pct, 1) + " %");
      row("printable", (Number(p.printable_ratio) * 100 || 0).toFixed(1) + " %");
      row("bytes", String(p.bytes != null ? p.bytes : "—"));
      row("talkers", (Array.isArray(p.talkers) && p.talkers.length) ? p.talkers.join(", ") : "—");
      card.appendChild(tbl);

      // inventory
      const inv = p.inventory || {};
      const keys = Object.keys(inv);
      if (keys.length) {
        card.appendChild(el("div", "hint", "Inventory (formatter · rate Hz · last seen s):"));
        const itbl = el("table", "kvtable");
        for (const k of keys) {
          const tr = document.createElement("tr");
          tr.appendChild(el("td", null, k));
          const info = inv[k] || {};
          tr.appendChild(el("td", null, num(info.rate_hz, 2) + " Hz · " + (info.last_seen_s == null ? "—" : num(info.last_seen_s, 1) + "s")));
          itbl.appendChild(tr);
        }
        card.appendChild(itbl);
      }

      // voltage: /api/diag does not expose voltage sensing -> honest "not installed" chip
      const vchip = el("div", "chip-muted", "voltage sensing not installed");
      vchip.style.marginTop = "8px";
      card.appendChild(vchip);

      grid.appendChild(card);
    }
  }

  // click-to-decode
  $("dec-btn").addEventListener("click", async () => {
    const line = $("dec-line").value;
    if (!line.trim()) { $("dec-out").textContent = ""; return; }
    const r = await postJson("/api/diag/decode", { line });
    renderDecode(r.data || {});
  });
  function renderDecode(d) {
    const out = $("dec-out"); out.textContent = "";
    const ck = el("span", "src " + (d.checksum_ok ? "src-live" : "src-off"), d.checksum_ok ? "CHECKSUM OK" : "CHECKSUM BAD/NA");
    out.appendChild(ck);
    const tbl = el("table", "kvtable"); tbl.style.marginTop = "8px";
    const row = (k, v) => { const tr = document.createElement("tr"); tr.appendChild(el("td", null, k)); tr.appendChild(el("td", null, String(v))); tbl.appendChild(tr); };
    if (d.error) row("error", d.error);
    if (d.note) row("note", d.note);
    if (d.sentence_type != null) row("sentence_type", d.sentence_type);
    if (d.talker != null) row("talker", d.talker);
    if (d.proprietary) row("proprietary", "yes");
    if (Array.isArray(d.raw_fields)) d.raw_fields.forEach((f, i) => row("field[" + i + "]", f));
    if (d.fields && typeof d.fields === "object") for (const k of Object.keys(d.fields)) row(k, d.fields[k]);
    out.appendChild(tbl);
  }

  // active TX controls
  function setActMsg(t, c) { const m = $("act-msg"); m.textContent = t || ""; m.className = "msg" + (c ? " " + c : ""); }
  function renderActOut(status, data) {
    const out = $("act-out"); out.textContent = "";
    const tbl = el("table", "kvtable");
    const row = (k, v) => { const tr = document.createElement("tr"); tr.appendChild(el("td", null, k)); tr.appendChild(el("td", null, typeof v === "object" ? JSON.stringify(v) : String(v))); tbl.appendChild(tr); };
    row("HTTP", status);
    if (data && typeof data === "object") for (const k of Object.keys(data)) row(k, data[k]);
    out.appendChild(tbl);
  }
  async function runAction(url, extra) {
    const slot = $("act-slot").value.trim();
    const confirm = $("act-confirm").value.trim();
    if (!slot) { setActMsg("Enter a slot id.", "err"); return; }
    const body = Object.assign({ slot, confirm }, extra || {});
    const r = await postJson(url, body);
    if (r.ok) setActMsg("OK", "ok"); else setActMsg("Refused: " + ((r.data && r.data.detail) || ("HTTP " + r.status)), "err");
    renderActOut(r.status, r.data);
  }
  $("act-sweep").addEventListener("click", () => runAction("/api/diag/baud-sweep"));
  $("act-send").addEventListener("click", () => runAction("/api/diag/send", { line: $("act-line").value }));
  $("act-loop").addEventListener("click", () => runAction("/api/diag/loopback"));
  // capture uses action, not confirm
  async function runCapture(action) {
    const slot = $("act-slot").value.trim();
    if (!slot) { setActMsg("Enter a slot id.", "err"); return; }
    const r = await postJson("/api/diag/capture", { slot, action });
    if (r.ok) setActMsg("Capture " + action + " OK", "ok"); else setActMsg("Refused: " + ((r.data && r.data.detail) || ("HTTP " + r.status)), "err");
    renderActOut(r.status, r.data);
  }
  $("act-cap-start").addEventListener("click", () => runCapture("start"));
  $("act-cap-stop").addEventListener("click", () => runCapture("stop"));

  /* =====================================================================
   *  SECURITY TAB (poll /api/security on show + slow refresh)
   * ===================================================================== */
  function startSecPoll() { pollSecurity(); secTimer = setInterval(pollSecurity, 10000); }
  function stopSecPoll() { if (secTimer) { clearInterval(secTimer); secTimer = null; } }
  async function pollSecurity() {
    let d;
    try { const res = await fetch("/api/security"); d = await res.json(); } catch (e) { return; }
    renderSecurity(d);
  }
  function boolTag(on, onText, offText) {
    const t = el("span", "src " + (on ? "src-live" : "src-off"), on ? (onText || "ENABLED") : (offText || "DISABLED"));
    return t;
  }
  function renderSecurity(d) {
    const tbl = $("sec-table"); tbl.textContent = "";
    const row = (k, node) => {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", null, k));
      const td = document.createElement("td");
      if (node instanceof Node) td.appendChild(node); else td.textContent = String(node);
      tr.appendChild(td); tbl.appendChild(tr);
    };
    row("TLS", "active (" + (d.tls || "internal") + " CA)");
    row("Reverse-proxy Basic auth", boolTag(!!d.caddy_basic));
    row("In-app Basic auth (defense-in-depth)", boolTag(!!d.app_basic));
    row("App bind", String(d.app_bind || "—"));
    row("Engine running", boolTag(!!d.running, "RUNNING", "STOPPED"));
    row("Uptime", (Number(d.uptime_s) || 0).toFixed(0) + " s");
    row("SSE subscribers", (d.subscribers != null ? d.subscribers : "—") + " / " + (d.max_subscribers != null ? d.max_subscribers : "—"));
    row("TCP tap host", String(d.tap_host || "—"));
    const hdrs = Array.isArray(d.headers) && d.headers.length ? d.headers.join(", ") : "none set by app (handled by reverse proxy)";
    row("Security headers", hdrs);

    // taps
    const tapsWrap = $("sec-taps"); tapsWrap.textContent = "";
    const taps = Array.isArray(d.taps) ? d.taps : [];
    if (!taps.length) { tapsWrap.appendChild(el("span", "hint", "No TCP taps enabled.")); return; }
    const t = el("table", "kvtable");
    const hr = document.createElement("tr"); hr.appendChild(el("td", null, "channel")); hr.appendChild(el("td", null, "endpoint")); t.appendChild(hr);
    for (const tap of taps) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", null, tap.channel));
      const td = document.createElement("td");
      const code = el("code", "inl", (d.tap_host || "127.0.0.1") + ":" + tap.port);
      td.appendChild(code);
      tr.appendChild(td); t.appendChild(tr);
    }
    tapsWrap.appendChild(t);
  }

  /* =====================================================================
   *  BOOTSTRAP
   * ===================================================================== */
  async function init() {
    try {
      const res = await fetch("/api/config");
      cfg = await res.json();
    } catch (e) {
      $("panes").appendChild(el("div", null, "Failed to load config: " + e.message));
      return;
    }
    const tcpHost = cfg.tcp_tap_host || "127.0.0.1";
    const channels = Array.isArray(cfg.channels) ? cfg.channels : [];
    const aggOn = !!(cfg.aggregate_tap && cfg.aggregate_tap.enabled);
    channelOrder = channels.map((c) => c.id);
    for (const ch of channels) {
      channelMeta[ch.id] = { role: String(ch.role || "").toLowerCase(), talker: ch.talker || "" };
      // A tap-only channel (no serial adapter) has no feed of its own to watch — its sentences
      // appear in the consolidated pane instead, so don't give it a dedicated pane.
      if (ch.tap_only && aggOn) continue;
      buildPane(ch, tcpHost);
    }
    if (aggOn) buildAggregatePane(cfg.aggregate_tap.port, tcpHost);
    buildConfigForms();
    buildConfigChannels();
    buildConfigSentences();
    loadRouteReplayIntoConfig();
    await loadAisTrafficIntoConfig();
    setMode(cfg.mode || "simulate");
    connectStream();
  }

  init();
})();
