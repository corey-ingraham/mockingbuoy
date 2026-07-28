"use strict";
(function () {
  const MAX_LINES = 300;
  const STALE_MS = 3500;
  const DEPTH_CAP = 180;    // 1 Hz depth samples => 3-minute history window
  let ALERT_DEPTH_M = 50;  // display-only shallow-water alert threshold (amber); A6: overridable via cfg-depth-alert (localStorage)
  try { const _ad = Number((window.localStorage && localStorage.getItem("mb.alertDepthM"))); if (Number.isFinite(_ad) && _ad > 0) ALERT_DEPTH_M = _ad; } catch (e) {}
  // Client-side conning temperature unit (display-only; temps never touch NMEA). Persisted to localStorage.
  let TEMP_UNIT = "C";
  try { const u = (window.localStorage && localStorage.getItem("mb.tempUnit")); if (u === "F" || u === "C") TEMP_UNIT = u; } catch (e) {}

  // Manual-field range table mirroring the server's _UPDATE_RANGES (client-side pre-check only).
  const RANGES = {
    lat: [-90, 90], lon: [-180, 180], sog_kn: [0, null], cog_deg: [0, 360],
    heading_true_deg: [0, 360], heading_mag_deg: [0, 360], mag_variation_deg: [-180, 180],
    altitude_m: [null, null], fix_quality: [0, null], satellites: [0, null], hdop: [0, null],
    stw_kn: [0, 100], depth_m: [0, 12000], rot_dpm: [-720, 720], wind_speed_kn: [0, 200],
    wind_dir_deg: [0, 360], sea_state: [0, 9], rudder_angle_deg: [-45, 45], set_deg: [0, 360],
    drift_kn: [0, 100],
  };
  // Fields the Save-as-defaults endpoint accepts (server allow-list). A3: widened with the 11
  // nav/GNSS keys so the per-card Save can persist them (fix_quality/satellites are ints server-side).
  const INITIAL_FIELDS = [
    "stw_kn","depth_m","rot_dpm","wind_speed_kn","wind_dir_deg","sea_state","rudder_angle_deg","set_deg","drift_kn",
    "lat","lon","sog_kn","cog_deg","heading_true_deg","heading_mag_deg","mag_variation_deg","altitude_m","fix_quality","satellites","hdop",
  ];

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
  let inPanes = {};                 // input_id -> live input pane state (Streams tab, auto mode only)
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
    return fmtDeg(Math.abs(Number(lat)), Number(lat) >= 0 ? "N" : "S");
  }
  function fmtLon(lon) {
    if (lon == null || !Number.isFinite(Number(lon))) return "---";
    return fmtDeg(Math.abs(Number(lon)), Number(lon) >= 0 ? "E" : "W");
  }
  // JPG-style position: 3-digit degrees, then decimal-minutes shown as two 3-digit groups
  function fmtDeg(a, h) {
    const d = Math.floor(a);
    const m6 = String(Math.min(599999, Math.round((a - d) * 60 * 10000))).padStart(6, "0");
    return String(d).padStart(3, "0") + "° " + m6.slice(0, 3) + " " + m6.slice(3) + " " + h;
  }
  function fmtUtc(iso) {
    if (!iso) return "--:--:--";
    const t = String(iso);
    const m = t.match(/T(\d{2}:\d{2}:\d{2})/);
    return m ? m[1] : t;
  }
  // same HH:MM:SS format as UTC, but in the browser's local timezone
  function fmtLocal(iso) {
    if (!iso) return "--:--:--";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "--:--:--";
    const p = (n) => (n < 10 ? "0" : "") + n;
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
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
    if (view === "streams") { updateInputSection(); }
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
      line.setAttribute("stroke", major ? "#7d8895" : (labels && labels.minorColor) || "#1a2330");
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
    g.setAttribute("transform", "rotate(" + Number(thetaDeg).toFixed(1) + " 150 195)");
    const line = g.querySelector("line"), head = g.querySelector("polygon");
    const tip = 195 - len;
    // stop the shaft at the arrowhead base (not the very point) so the line never pokes past the tip
    if (line) line.setAttribute("y2", (tip + 9).toFixed(1));
    if (head) head.setAttribute("points", "150," + tip.toFixed(1) + " 144," + (tip + 10).toFixed(1) + " 156," + (tip + 10).toFixed(1));
  }
  // Ship-schematic horizontal (athwartships) callout arrow at row y; +v = starboard (right).
  function setLatArrow(id, v, y) {
    const g = $(id); if (!g) return;
    // show the direction tip as soon as there's any real walk (tight threshold, was 0.05)
    if (!Number.isFinite(v) || Math.abs(v) < 0.005) { g.setAttribute("opacity", "0"); return; }
    g.setAttribute("opacity", "1");
    const dir = v >= 0 ? 1 : -1;                        // +v = starboard (right)
    // just a solid arrowhead beside the walk box (no shaft/base); magnitude is in the kt readout
    const xBase = 150 + dir * 30;                       // box edge + small gap
    const xTip = xBase + dir * 15;                      // points the way she walks
    const head = g.querySelector("polygon");
    if (head) head.setAttribute("points", xTip.toFixed(1) + "," + y + " " + xBase.toFixed(1) + "," + (y - 7) + " " + xBase.toFixed(1) + "," + (y + 7));
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
    // finer graduations (every 10deg) + 0/90/180/270 relative-bearing numbers on the majors
    const nums = { 0: "0", 90: "90", 180: "180", 270: "270" };
    buildDialTicks(g, 100, 100, 94, 88, 80, 10, 90, { r: 66, minorColor: "#4a5560", fn: (deg) => nums[deg] || "" });
  })();
  // wind TRUE dial: north-up card with cardinal labels + finer graduations
  (function buildWtdTicks() {
    const g = $("wtd-ticks");
    if (!g) return;
    const cards = { 0: "N", 90: "E", 180: "S", 270: "W" };
    buildDialTicks(g, 80, 80, 74, 70, 60, 10, 90, { r: 50, minorColor: "#4a5560", fn: (deg) => cards[deg] || "" });
  })();
  // seed the depth-alert display constant (must match ALERT_DEPTH_M)
  (function setDepthAlert() {
    const n = $("depth-alert");
    if (n) n.textContent = ALERT_DEPTH_M.toFixed(1);
  })();
  // Heading arc: a 0-360 tick ring hung about a centre far below the svg (350,700), r=620, so the
  // top of the ring reads as a shallow curved tape. Majors every 10deg carry a 3-digit number.
  // repaint rotates #hdg-arc by -heading so the current heading sits under the fixed centre caret;
  // the big #heading-big shows the exact value.
  const hdgArcNums = [];  // arc-scale number glyphs; repaint counter-rotates each to stay upright
  (function buildHeadingArc() {
    const g = $("hdg-arc");
    if (!g) return;
    const NS = "http://www.w3.org/2000/svg";
    const cx = 350, cy = 700, R = 620;
    const pt = (deg, r) => { const a = (deg - 90) * Math.PI / 180; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; };
    for (let deg = 0; deg < 360; deg += 2) {
      const major = deg % 10 === 0;
      const p1 = pt(deg, R), p2 = pt(deg, R - (major ? 20 : 11));
      const ln = document.createElementNS(NS, "line");
      ln.setAttribute("x1", p1[0].toFixed(1)); ln.setAttribute("y1", p1[1].toFixed(1));
      ln.setAttribute("x2", p2[0].toFixed(1)); ln.setAttribute("y2", p2[1].toFixed(1));
      ln.setAttribute("stroke", major ? "#dfe6ec" : "#6b7580");
      ln.setAttribute("stroke-width", major ? "2" : "1.2");
      g.appendChild(ln);
      if (major) {
        const lp = pt(deg, R - 36);
        const t = document.createElementNS(NS, "text");
        const lx = lp[0].toFixed(1), ly = (lp[1] + 6).toFixed(1);
        t.setAttribute("x", lx); t.setAttribute("y", ly);
        t.setAttribute("fill", "#c3ccd4"); t.setAttribute("font-size", "17");
        t.setAttribute("font-family", "Segoe UI, system-ui, sans-serif");
        t.setAttribute("font-weight", "700"); t.setAttribute("text-anchor", "middle");
        t.textContent = (deg < 10 ? "00" : deg < 100 ? "0" : "") + deg;
        t.dataset.lx = lx; t.dataset.ly = ly;
        g.appendChild(t);
        hdgArcNums.push(t);
      }
    }
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
  // ship-schematic rudder fan: graduated protractor below the stern pivot (150,372), 0deg straight
  // down; sampled polyline base arc (flag-independent) + 5deg minor / 20deg major ticks + hard-over red.
  (function buildShipRudderScale() {
    const g = $("ship-rud-scale");
    if (!g) return;
    const NS = "http://www.w3.org/2000/svg";
    const cx = 150, cy = 372, R = 48, SPAN = 40;
    const pt = (a, r) => [cx + r * Math.sin(a * Math.PI / 180), cy + r * Math.cos(a * Math.PI / 180)];
    let d = "";
    for (let a = -SPAN; a <= SPAN + 0.001; a += 2) { const p = pt(a, R); d += (d ? " " : "") + p[0].toFixed(1) + "," + p[1].toFixed(1); }
    const arc = document.createElementNS(NS, "polyline");
    arc.setAttribute("points", d); arc.setAttribute("fill", "none");
    arc.setAttribute("stroke", "#8792a0"); arc.setAttribute("stroke-width", "1.6");
    g.appendChild(arc);
    for (let a = -SPAN; a <= SPAN + 0.001; a += 5) {
      const major = a % 15 === 0, hard = Math.abs(a) >= 35;
      const p1 = pt(a, R), p2 = pt(a, R + (major ? 8 : 4));
      const ln = document.createElementNS(NS, "line");
      ln.setAttribute("x1", p1[0].toFixed(1)); ln.setAttribute("y1", p1[1].toFixed(1));
      ln.setAttribute("x2", p2[0].toFixed(1)); ln.setAttribute("y2", p2[1].toFixed(1));
      ln.setAttribute("stroke", hard ? "#ff4d4d" : major ? "#c3ccd4" : "#6b7580");
      ln.setAttribute("stroke-width", major ? "1.8" : "1");
      g.appendChild(ln);
      // small degree numbers on the big (0/15/30) ticks
      if (major && Math.abs(a) <= 30) {
        const lp = pt(a, R + 16);
        const t = document.createElementNS(NS, "text");
        t.setAttribute("x", lp[0].toFixed(1)); t.setAttribute("y", (lp[1] + 3).toFixed(1));
        t.setAttribute("fill", "#8792a0"); t.setAttribute("font-size", "9");
        t.setAttribute("font-family", "Segoe UI, system-ui, sans-serif"); t.setAttribute("text-anchor", "middle");
        t.textContent = String(Math.abs(a));
        g.appendChild(t);
      }
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

  // Single main-engine AHEAD/ASTERN tachometer (order vs actual) drawn once into a 220x130 top
  // semicircle: glossy face + metallic bezel + glass highlight + green/amber/red ahead bands + red
  // astern band + ticks, then a shadowed needle (ACTUAL rpm, "<id>-needle") and an amber telegraph
  // marker on the outer rim (ORDERED rpm, "<id>-order"). STOP at 12 o'clock; ahead sweeps right
  // (uniform 0.75 deg/rpm, 0..120 -> 0..+90 deg), astern sweeps left (0..-80 -> 0..-60 deg). Amber =
  // display-only. Angle from vertical-up, +clockwise: pt = (cx+R*sin, cy-R*cos).
  const TACH_CX = 110, TACH_CY = 118, TACH_DPR = 0.75; // deg per rpm
  const TACH_A_AHEAD = 90, TACH_A_ASTERN = -60;         // needle sweep limits (deg)
  const _NS = "http://www.w3.org/2000/svg";
  const _rad = (a) => (a * Math.PI) / 180;
  const _tachPt = (r, a) => [TACH_CX + r * Math.sin(_rad(a)), TACH_CY - r * Math.cos(_rad(a))];
  // stroked arc path between two angles (deg) at radius r; sweep follows the angle direction
  function _tachArc(r, a1, a2, stroke, width, cap) {
    const [x1, y1] = _tachPt(r, a1), [x2, y2] = _tachPt(r, a2);
    const large = Math.abs(a2 - a1) > 180 ? 1 : 0, sweep = a2 > a1 ? 1 : 0;
    const p = document.createElementNS(_NS, "path");
    p.setAttribute("d", `M${x1.toFixed(1)} ${y1.toFixed(1)} A${r} ${r} 0 ${large} ${sweep} ${x2.toFixed(1)} ${y2.toFixed(1)}`);
    p.setAttribute("fill", "none"); p.setAttribute("stroke", stroke); p.setAttribute("stroke-width", String(width));
    if (cap) p.setAttribute("stroke-linecap", cap);
    return p;
  }
  // rpm -> needle/marker angle; non-finite (missing/partial payload) -> 0 (STOP), matching every
  // other rotate() transform in this file (avoids writing an invalid "rotate(NaN ...)").
  const _tachAngle = (rpm) => { const r = Number(rpm); return Number.isFinite(r) ? Math.max(TACH_A_ASTERN, Math.min(TACH_A_AHEAD, r * TACH_DPR)) : 0; };
  function buildEngineTach(id) {
    const svg = $(id);
    if (!svg) return;
    const R_FACE = 92, R_BEZEL = 94, R_GUTTER = 97, R_ZONE = 82, R_TICK = 76, R_LAB = 62, R_ORDER = 90;
    // glossy face — closed top semicircle (flat bottom at cy), same faceGrad as every other dial
    const [flx, fly] = _tachPt(R_FACE, -90), [frx, fry] = _tachPt(R_FACE, 90);
    const face = document.createElementNS(_NS, "path");
    face.setAttribute("d", `M${flx.toFixed(1)} ${fly.toFixed(1)} A${R_FACE} ${R_FACE} 0 0 1 ${frx.toFixed(1)} ${fry.toFixed(1)} Z`);
    face.setAttribute("fill", "url(#faceGrad)");
    svg.appendChild(face);
    // glass reflection highlight (bare ellipse near the top, sized to stay within the face) — WTD-dial style
    const glass = document.createElementNS(_NS, "ellipse");
    glass.setAttribute("cx", String(TACH_CX)); glass.setAttribute("cy", String(TACH_CY - 60));
    glass.setAttribute("rx", "60"); glass.setAttribute("ry", "34"); glass.setAttribute("fill", "url(#glassGrad)");
    svg.appendChild(glass);
    // metallic bezel + dark gutter rings (arc only, over the face rim)
    svg.appendChild(_tachArc(R_GUTTER, -90, 90, "#05080c", 4));
    svg.appendChild(_tachArc(R_BEZEL, -90, 90, "url(#bezelGrad)", 3));
    // operating bands — ahead green/amber/red (rpm), astern muted red
    for (const [rf, rt, col] of [[0, 100, "#2ea043"], [100, 110, "#d9a520"], [110, 120, "#e5484d"]]) {
      svg.appendChild(_tachArc(R_ZONE, rf * TACH_DPR, rt * TACH_DPR, col, 5, "round"));
    }
    { const astern = _tachArc(R_ZONE, 0, TACH_A_ASTERN, "#e5484d", 5, "round"); astern.setAttribute("opacity", "0.55"); svg.appendChild(astern); }
    // ticks + monospace labels: ahead 0/30/60/90/120, astern 40/80 (magnitude)
    const ticks = [
      [0, "STOP"], [30, "30"], [60, "60"], [90, "90"], [120, "120"], [-40, "40"], [-80, "80"],
    ];
    for (const [rpm, txt] of ticks) {
      const a = rpm * TACH_DPR, [ox, oy] = _tachPt(R_TICK, a), [ix, iy] = _tachPt(R_TICK - 8, a);
      const ln = document.createElementNS(_NS, "line");
      ln.setAttribute("x1", ox.toFixed(1)); ln.setAttribute("y1", oy.toFixed(1));
      ln.setAttribute("x2", ix.toFixed(1)); ln.setAttribute("y2", iy.toFixed(1));
      ln.setAttribute("stroke", "#aab4bf"); ln.setAttribute("stroke-width", "1.5");
      svg.appendChild(ln);
      const [lx, ly] = _tachPt(R_LAB, a);
      // near-horizontal extreme labels (e.g. 120 at +90deg) would straddle the flat baseline; lift them
      const lift = Math.abs(a) >= 85 ? 8 : 0;
      const t = document.createElementNS(_NS, "text");
      t.setAttribute("x", lx.toFixed(1)); t.setAttribute("y", (ly + 3 - lift).toFixed(1));
      t.setAttribute("fill", txt === "STOP" ? "#c6ced6" : "#aab4bf");
      t.setAttribute("font-size", txt === "STOP" ? "8.5" : "9.5"); t.setAttribute("font-weight", "700");
      t.setAttribute("font-family", "monospace"); t.setAttribute("text-anchor", "middle");
      t.textContent = txt;
      svg.appendChild(t);
    }
    // telegraph ORDER marker — amber chevron on the outer rim, larger radius than the needle tip so it
    // stays visible even when order and actual coincide; rotated about the hub by setEngineTach
    const order = document.createElementNS(_NS, "polygon");
    order.setAttribute("id", id + "-order");
    order.setAttribute("points", `${TACH_CX - 5},${TACH_CY - R_ORDER - 7} ${TACH_CX + 5},${TACH_CY - R_ORDER - 7} ${TACH_CX},${TACH_CY - R_ORDER + 3}`);
    order.setAttribute("fill", "#ff9500"); order.setAttribute("stroke", "#1a1204"); order.setAttribute("stroke-width", "0.8");
    order.setAttribute("filter", "url(#needleShadow)");
    svg.appendChild(order);
    // ACTUAL-rpm needle — tapered 3D pointer + counterweight tail, shared drop shadow (JPG dial style)
    const needle = document.createElementNS(_NS, "g");
    needle.setAttribute("id", id + "-needle"); needle.setAttribute("filter", "url(#needleShadow)");
    const blade = document.createElementNS(_NS, "polygon");
    blade.setAttribute("points", `${TACH_CX},${TACH_CY - 78} ${TACH_CX - 3.4},${TACH_CY} ${TACH_CX + 3.4},${TACH_CY}`);
    blade.setAttribute("fill", "#eef3f7");
    needle.appendChild(blade);
    const tail = document.createElementNS(_NS, "polygon");
    tail.setAttribute("points", `${TACH_CX - 3},${TACH_CY} ${TACH_CX + 3},${TACH_CY} ${TACH_CX},${TACH_CY + 12}`);
    tail.setAttribute("fill", "#9aa4b0");
    needle.appendChild(tail);
    svg.appendChild(needle);
    // chrome hub + specular dot — matches the wind-rose hub
    const hub = document.createElementNS(_NS, "circle");
    hub.setAttribute("cx", String(TACH_CX)); hub.setAttribute("cy", String(TACH_CY)); hub.setAttribute("r", "8"); hub.setAttribute("fill", "url(#hubGrad)");
    svg.appendChild(hub);
    const spec = document.createElementNS(_NS, "circle");
    spec.setAttribute("cx", String(TACH_CX - 2.5)); spec.setAttribute("cy", String(TACH_CY - 2.5)); spec.setAttribute("r", "1.8"); spec.setAttribute("fill", "#ffffff"); spec.setAttribute("fill-opacity", "0.9");
    svg.appendChild(spec);
  }
  // Rotate the needle to ACTUAL signed rpm and the order marker to ORDERED signed rpm (deg cw about the hub).
  function setEngineTach(id, rpmSigned, rpmOrdered) {
    const nd = $(id + "-needle"), or = $(id + "-order");
    if (nd) nd.setAttribute("transform", `rotate(${_tachAngle(rpmSigned).toFixed(1)} ${TACH_CX} ${TACH_CY})`);
    if (or) or.setAttribute("transform", `rotate(${_tachAngle(rpmOrdered).toFixed(1)} ${TACH_CX} ${TACH_CY})`);
  }
  // Slim horizontal LOAD bar (0..110 %) with zoned underlay + amber fill + white pointer. viewBox
  // 220x16 stretched (preserveAspectRatio none); "<id>-fill" width + "<id>-mark" x set by setLoadBar.
  const LOAD_X0 = 3, LOAD_W = 214, LOAD_MAX = 110;
  function buildLoadBar(id) {
    const svg = $(id);
    if (!svg) return;
    // recessed glass-black track (same faceGrad/bezelGrad as the other recessed indicators)
    const track = document.createElementNS(_NS, "rect");
    track.setAttribute("x", "1"); track.setAttribute("y", "1"); track.setAttribute("width", "218"); track.setAttribute("height", "14");
    track.setAttribute("rx", "3"); track.setAttribute("fill", "url(#faceGrad)"); track.setAttribute("stroke", "url(#bezelGrad)"); track.setAttribute("stroke-width", "1");
    svg.appendChild(track);
    // faint zone underlay so the amber/red bands read even below the current fill
    for (const [zf, zt, zc] of [[0, 85, "#2ea043"], [85, 100, "#d9a520"], [100, 110, "#e5484d"]]) {
      const z = document.createElementNS(_NS, "rect");
      z.setAttribute("x", (LOAD_X0 + (zf / LOAD_MAX) * LOAD_W).toFixed(1)); z.setAttribute("y", "12");
      z.setAttribute("width", (((zt - zf) / LOAD_MAX) * LOAD_W).toFixed(1)); z.setAttribute("height", "3");
      z.setAttribute("fill", zc); z.setAttribute("opacity", "0.6");
      svg.appendChild(z);
    }
    const fill = document.createElementNS(_NS, "rect");
    fill.setAttribute("id", id + "-fill");
    fill.setAttribute("x", String(LOAD_X0)); fill.setAttribute("y", "2.5"); fill.setAttribute("width", "0"); fill.setAttribute("height", "9");
    fill.setAttribute("rx", "1.5"); fill.setAttribute("fill", "url(#engFill)");
    svg.appendChild(fill);
    const mark = document.createElementNS(_NS, "rect");
    mark.setAttribute("id", id + "-mark");
    mark.setAttribute("x", String(LOAD_X0)); mark.setAttribute("y", "1"); mark.setAttribute("width", "2"); mark.setAttribute("height", "14");
    mark.setAttribute("fill", "#dfe6ec");
    svg.appendChild(mark);
  }
  function setLoadBar(id, pct) {
    const frac = Number.isFinite(Number(pct)) ? Math.max(0, Math.min(1, Number(pct) / LOAD_MAX)) : 0;
    const w = frac * LOAD_W;
    const fill = $(id + "-fill"); if (fill) fill.setAttribute("width", w.toFixed(1));
    const mark = $(id + "-mark"); if (mark) mark.setAttribute("x", (LOAD_X0 + w - 1).toFixed(1));
  }
  // Autopilot linear deviation indicator: 220x44 with a centre-zero baseline, ±half labels and a
  // diamond marker "<id>-mark" placed at x=110+clamp(v/half,-1,1)*100 by setLinearMarker. Amber.
  function buildLinearIndicator(id, halfRange, unit) {
    const svg = $(id);
    if (!svg) return;
    const NS = "http://www.w3.org/2000/svg";
    const cx = 110, cy = 22, x0 = 10, x1 = 210;
    // glossy recessed track (JPG style)
    const track = document.createElementNS(NS, "rect");
    track.setAttribute("x", String(x0 - 3)); track.setAttribute("y", String(cy - 7));
    track.setAttribute("width", String(x1 - x0 + 6)); track.setAttribute("height", "14");
    track.setAttribute("rx", "7"); track.setAttribute("fill", "url(#faceGrad)");
    track.setAttribute("stroke", "url(#bezelGrad)"); track.setAttribute("stroke-width", "1");
    svg.appendChild(track);
    // light-green deviation fill between centre-zero and the marker (JPG style), sized by setLinearMarker
    const fillR = document.createElementNS(NS, "rect");
    fillR.setAttribute("id", id + "-fill");
    fillR.setAttribute("x", String(cx)); fillR.setAttribute("y", String(cy - 5));
    fillR.setAttribute("width", "0"); fillR.setAttribute("height", "10");
    fillR.setAttribute("fill", "#00e07a"); fillR.setAttribute("opacity", "0.32");
    svg.appendChild(fillR);
    // graduations: minor every 0.1, major every 0.5
    for (let i = -10; i <= 10; i++) {
      const f = i / 10, x = cx + f * 100, big = i % 5 === 0;
      const ln = document.createElementNS(NS, "line");
      ln.setAttribute("x1", x.toFixed(1)); ln.setAttribute("y1", String(cy - (big ? 7 : 4)));
      ln.setAttribute("x2", x.toFixed(1)); ln.setAttribute("y2", String(cy + (big ? 7 : 4)));
      ln.setAttribute("stroke", big ? "#8792a0" : "#3a4655"); ln.setAttribute("stroke-width", big ? "1.4" : "0.8");
      svg.appendChild(ln);
    }
    const mklabel = (x, txt, anchor) => {
      const t = document.createElementNS(NS, "text");
      t.setAttribute("x", String(x)); t.setAttribute("y", String(cy + 18));
      t.setAttribute("fill", "#7d8895"); t.setAttribute("font-size", "8");
      t.setAttribute("font-family", "monospace"); t.setAttribute("text-anchor", anchor);
      t.textContent = txt; svg.appendChild(t);
    };
    mklabel(x0, "-" + halfRange + unit, "start");
    mklabel(cx - 50, "-" + (halfRange / 2) + unit, "middle");
    mklabel(cx, "0", "middle");
    mklabel(cx + 50, "+" + (halfRange / 2) + unit, "middle");
    mklabel(x1, "+" + halfRange + unit, "end");
    // sleek rounded-diamond marker (gradient fill + dark rim + drop shadow), moved by transform
    const mk = document.createElementNS(NS, "path");
    mk.setAttribute("id", id + "-mark");
    mk.setAttribute("d", "M0,-8.5 C3.4,-4.8 5.6,-2.4 6.6,0 C5.6,2.4 3.4,4.8 0,8.5 C-3.4,4.8 -5.6,2.4 -6.6,0 C-5.6,-2.4 -3.4,-4.8 0,-8.5 Z");
    mk.setAttribute("fill", "url(#diaGrad)");
    mk.setAttribute("stroke", "#0a4527"); mk.setAttribute("stroke-width", "0.9");
    mk.setAttribute("filter", "url(#needleShadow)");
    mk.setAttribute("transform", "translate(" + cx + " " + cy + ")");
    svg.appendChild(mk);
  }
  function setLinearMarker(id, v, half) {
    const mk = $(id);
    if (!mk) return;
    const cx = 110, cy = 22;
    const frac = Number.isFinite(Number(v)) ? Math.max(-1, Math.min(1, Number(v) / half)) : 0;
    const x = cx + frac * 100;
    mk.setAttribute("transform", "translate(" + x.toFixed(1) + " " + cy + ")");
    const fill = $(id.replace(/-mark$/, "-fill"));
    if (fill) { fill.setAttribute("x", Math.min(cx, x).toFixed(1)); fill.setAttribute("width", Math.abs(x - cx).toFixed(1)); }
  }
  // build the single main-engine bars + the two autopilot linear indicators once (each guards a missing target)
  (function buildPropulsionGauges() {
    buildEngineTach("prop-main");
    buildLoadBar("prop-load-bar");
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
      // heading tape: scroll the scale so the current heading sits under the centre caret
      const hdgArc = $("hdg-arc");
      if (hdgArc) hdgArc.setAttribute("transform", "rotate(" + (-hdg).toFixed(2) + " 350 700)");
      // counter-rotate each arc number by +hdg about its own anchor so it stays upright/horizontal
      const hArcR = hdg.toFixed(2);
      for (let i = 0; i < hdgArcNums.length; i++) {
        const t = hdgArcNums[i];
        t.setAttribute("transform", "rotate(" + hArcR + " " + t.dataset.lx + " " + t.dataset.ly + ")");
      }
      const headingBig = $("heading-big");
      if (headingBig) { const hv = ((Math.round(hdg) % 360) + 360) % 360; headingBig.textContent = (hv < 10 ? "00" : hv < 100 ? "0" : "") + hv; }
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
    // prominent live heading centred over the tape
    const cogBig = $("cog-big");
    if (cogBig && Number.isFinite(cog)) cogBig.textContent = num(cog, 0);

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
    // engine-order telegraph (display-only, sim.engine_order_pct): AHEAD → green forward vectors above
    // the fuel box; ASTERN (order < -1%) → suppress the forward vectors and raise the red stern arrow.
    const ppEng = Number(sim.engine_order_pct);
    const asternEng = Number.isFinite(ppEng) && ppEng < -1;
    // length capped so the arrowhead stays in the gap above the BUNKERS box and never reaches the
    // bow-walk box (pivot y=195, bunkers box top y=164, bow box bottom y=124 -> keep tip >= ~132)
    setShipVec("ship-vec-cog", trackOk ? shipCog - shipHdg : 0, (!asternEng && trackOk) ? Math.min(63, shipSog * 9) : 0);
    setShipVec("ship-vec-cur", curOk ? setDeg - shipHdg : 0, (!asternEng && curOk) ? Math.min(58, driftKn * 18) : 0);
    const asternG = $("ship-astern-arrow");
    if (asternG) asternG.setAttribute("opacity", asternEng ? "1" : "0");
    // athwartships (docking) lateral speeds recovered from ground track + yaw; L=30 m, midships pivot
    const dlt = trackOk ? wrap180(shipCog - shipHdg) : 0;
    const dRad = dlt * Math.PI / 180;
    const vLat = trackOk ? shipSog * Math.sin(dRad) : 0;
    const vFwd = trackOk ? shipSog * Math.cos(dRad) : 0;
    const tang = rot * Math.PI / (180 * 60) * 15 * 1.9438; // yaw contribution at L/2, m/s -> kn
    const vBow = vLat + tang, vStern = vLat - tang;
    setLatArrow("ship-abow", vBow, 111);
    setLatArrow("ship-astern", vStern, 281);
    // bow/stern lateral walk shown as a always-populated kt readout in the hull boxes
    setTxt("ship-vbow", Math.abs(vBow).toFixed(2) + " kt");
    setTxt("ship-vstern", Math.abs(vStern).toFixed(2) + " kt");
    setTxt("ship-vfwd", num(vFwd, 1));
    // rudder blade at the ship stern (graphic mirrors the helm gauge; negate so +stbd swings right)
    const shipRudBlade = $("ship-rud-blade");
    if (shipRudBlade && Number.isFinite(rud)) shipRudBlade.setAttribute("transform", "rotate(" + (-rud).toFixed(1) + " 150 372)");
    setTxt("ship-rud-val", num(s.rudder_angle_deg, 1));

    // fuel — BUNKER gauge (amber = display-only, from s.sim): total tonnes + capacity fill bar,
    // plus the voyage row (economy / endurance / range). t/HR now lives in the Propulsion panel.
    { const ft = Number(sim.fuel_total_l); setTxt("fuel-total", Number.isFinite(ft) ? Math.round(ft).toLocaleString("en-US") : "----"); }
    { const pct = Number(sim.fuel_pct);
      const frac = Number.isFinite(pct) ? Math.max(0, Math.min(1, pct / 100)) : 0;
      const bar = $("fuel-bar"); if (bar) bar.setAttribute("width", (frac * 68).toFixed(1));
      setTxt("fuel-pct", Number.isFinite(pct) ? Math.round(pct) + "%" : "--%"); }
    setTxt("fuel-pernm", sim.fuel_per_nm_l == null ? "---" : num(sim.fuel_per_nm_l, 2));
    // endurance/range: order and sog are decoupled inputs, so a low telegraph at speed makes these
    // huge — cap the DISPLAY so the narrow voyage cells never overflow (999+ d; k / M for range).
    setTxt("fuel-endur", sim.fuel_endurance_days == null ? "---" : (sim.fuel_endurance_days >= 999 ? "999+" : num(sim.fuel_endurance_days, 1)));
    { const rn = Number(sim.fuel_range_nm);
      let s = "----";
      if (sim.fuel_range_nm != null && Number.isFinite(rn)) {
        s = rn >= 1e6 ? Math.round(rn / 1e6) + "M"
          : rn >= 1e4 ? Math.round(rn / 1e3) + "k"
          : Math.round(rn).toLocaleString("en-US");
      }
      setTxt("fuel-range", s); }

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

    // environment (amber = display-only, from s.sim). Temps convert C->F for display only; the
    // unit-span text is flipped by onTempUnitChange. NMEA is unaffected (temps never on the wire).
    const cvt = (c) => (TEMP_UNIT === "F" && c != null && Number.isFinite(Number(c))) ? Number(c) * 9 / 5 + 32 : c;
    setTxt("env-wtemp", num(cvt(sim.water_temp_c), 1));
    setTxt("env-atemp", num(cvt(sim.air_temp_c), 1));
    setTxt("env-hum", num(sim.humidity_pct, 0));
    setTxt("env-press", num(sim.pressure_hpa, 0));

    // propulsion — single main-engine ahead/astern tach (amber = display-only, from s.sim)
    { const ordered = Number(sim.rpm_ordered);
      const rpmSigned = (Number(sim.engine_order_pct) < 0 ? -Number(sim.rpm) : Number(sim.rpm));
      setEngineTach("prop-main", rpmSigned, Number.isFinite(ordered) ? ordered : 0); }
    setLoadBar("prop-load-bar", sim.load_pct);
    { const r = Number(sim.rpm); setTxt("prop-rpm", Number.isFinite(r) ? Math.round(r).toLocaleString("en-US") : "----"); }
    setTxt("prop-load", num(sim.load_pct, 0));
    setTxt("prop-power", num(sim.shaft_power_mw, 1));
    setTxt("prop-fuel", num(sim.fuel_rate_lph, 1));
    // ENGINE ORDER telegraph (display-only, from sim.engine_order_pct): |o|<1 → STOP (amber); else band
    // by magnitude (≥80 FULL, 55-80 HALF, 30-55 SLOW, else DEAD SLOW) with AHEAD (green)/ASTERN (red) suffix.
    { const o = Number(sim.engine_order_pct);
      let order = "STOP", cls = "sim";
      if (Number.isFinite(o) && Math.abs(o) >= 1) {
        const m = Math.abs(o);
        const band = m >= 80 ? "FULL" : m >= 55 ? "HALF" : m >= 30 ? "SLOW" : "DEAD SLOW";
        if (o > 0) { order = band + " AHEAD"; cls = "live"; }   // green via .stat-pill.live
        else { order = band + " ASTERN"; cls = "astern"; }      // red via .stat-pill.astern
      }
      const po = $("prop-order"); if (po) { po.textContent = order; po.className = "stat-pill " + cls; } }

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
    { const cg = Number(s.cog_deg); setTxt("pri-cog", Number.isFinite(cg) ? String((((Math.round(cg) % 360) + 360) % 360)).padStart(3, "0") : "---"); }
    setTxt("ro-hdg", num(s.heading_true_deg, 0) + " / " + num(s.heading_mag_deg, 0));
    // split the hemisphere letter into its own fixed slot so the centred digits line up row-to-row
    { const v = fmtLat(s.lat), m = v.match(/^(.*) ([NS])$/); setTxt("ro-lat", m ? m[1] : v); setTxt("ro-lat-h", m ? m[2] : ""); }
    { const v = fmtLon(s.lon), m = v.match(/^(.*) ([EW])$/); setTxt("ro-lon", m ? m[1] : v); setTxt("ro-lon-h", m ? m[2] : ""); }
    setTxt("ro-depth", num(s.depth_m, 1));
    setTxt("ro-utc", fmtUtc(s.utc));
    setTxt("ro-local", fmtLocal(s.utc));
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
    const x0 = 34, y0 = 24, x1 = 286, y1 = 162, w = x1 - x0, h = y1 - y0;
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
    // surface-to-seabed scale: 0 m fixed at the top; axis max slides to a nice ceiling below the deepest sample
    const dataMax = Math.max.apply(null, depthHistory.concat([ALERT_DEPTH_M]));
    const mn = 0;
    const mx = Math.max(20, Math.ceil((dataMax * 1.6) / 10) * 10);
    const span = mx - mn;
    const xstep = w / (DEPTH_CAP - 1);
    const xOf = (j) => x1 - (n - 1 - j) * xstep;      // newest at right
    const yOf = (depth) => y0 + (depth - mn) / span * h; // inverted: deeper = lower
    // horizontal grid + depth labels across the scaled window (JPG-style y axis)
    const GRID = 5;
    for (let i = 0; i <= GRID; i++) {
      const yy = y0 + (i / GRID) * h;
      const gl = mk("line");
      gl.setAttribute("x1", String(x0)); gl.setAttribute("x2", String(x1));
      gl.setAttribute("y1", yy.toFixed(1)); gl.setAttribute("y2", yy.toFixed(1));
      gl.setAttribute("stroke", "#1c2530"); gl.setAttribute("stroke-width", "0.6");
      dyn.appendChild(gl);
      // depth labels on the RIGHT of the plot (0 at the top, JPG style)
      label(x1 + 5, yy + 3, (mn + (i / GRID) * span).toFixed(0), "start");
    }
    // organic ocean-surface profile (two combined waves); drawn as a white line below, over the water fill
    const surfY = (xx) => y0 + Math.sin((xx - x0) / 9) * 1.7 + Math.sin((xx - x0) / 4.2) * 0.9;
    // small shadowed side-view ship (pitch-gauge silhouette family) sitting ON the surface, top-right
    const sx = x1 - 34, sy = surfY(x1 - 34) - 3; // lift so the hull rides higher on the surface
    const ship = mk("path");
    ship.setAttribute("d",
      // container-ship side profile (shadow): long low hull, raked bow (right), transom stern (left)
      "M" + (sx - 22) + "," + (sy - 2) + " L" + (sx + 15) + "," + (sy - 2) + " L" + (sx + 25) + "," + (sy - 4) + " L" + (sx + 17) + "," + (sy + 3) + " L" + (sx - 20) + "," + (sy + 3) + " Z" +
      // aft accommodation block + funnel (toward the stern, left)
      " M" + (sx - 20) + "," + (sy - 2) + " L" + (sx - 20) + "," + (sy - 11) + " L" + (sx - 12) + "," + (sy - 11) + " L" + (sx - 12) + "," + (sy - 2) + " Z" +
      " M" + (sx - 17) + "," + (sy - 11) + " L" + (sx - 17) + "," + (sy - 15) + " L" + (sx - 14) + "," + (sy - 15) + " L" + (sx - 14) + "," + (sy - 11) + " Z");
    ship.setAttribute("fill", "#5a6675"); ship.setAttribute("opacity", "0.82");
    // forward mast
    const mast = mk("line");
    mast.setAttribute("x1", (sx + 8).toFixed(1)); mast.setAttribute("y1", (sy - 2).toFixed(1));
    mast.setAttribute("x2", (sx + 8).toFixed(1)); mast.setAttribute("y2", (sy - 10).toFixed(1));
    mast.setAttribute("stroke", "#5a6675"); mast.setAttribute("stroke-width", "0.8"); mast.setAttribute("opacity", "0.82");
    dyn.appendChild(ship); dyn.appendChild(mast);
    // seabed trace points (surface at top → this depth), left→right
    const tp = [];
    for (let j = 0; j < n; j++) tp.push([xOf(j), yOf(depthHistory[j])]);
    const traceStr = tp.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
    // black seabed below the trace, down to the graph floor
    const seabed = mk("polygon");
    seabed.setAttribute("points", traceStr + " " + tp[n - 1][0].toFixed(1) + "," + y1 + " " + tp[0][0].toFixed(1) + "," + y1);
    seabed.setAttribute("fill", "#2b333d");
    dyn.appendChild(seabed);
    // light-blue water column between the surface line and the seabed trace
    const waterTop = tp.map((p) => p[0].toFixed(1) + "," + surfY(p[0]).toFixed(1)).join(" ");
    const waterBot = tp.slice().reverse().map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
    const water = mk("polygon");
    water.setAttribute("points", waterTop + " " + waterBot);
    water.setAttribute("fill", "#2f78b4"); water.setAttribute("opacity", "0.26");
    dyn.appendChild(water);
    // seabed trace (white)
    const line = mk("polyline");
    line.setAttribute("id", "depth-line");
    line.setAttribute("points", traceStr);
    line.setAttribute("fill", "none"); line.setAttribute("stroke", "#f2f6fa"); line.setAttribute("stroke-width", "1.6");
    dyn.appendChild(line);
    // ocean-surface line (white, wavy) on top of the water fill
    let wave = "";
    for (let wx = x0; wx <= x1; wx += 4) wave += (wx > x0 ? " " : "") + wx.toFixed(1) + "," + surfY(wx).toFixed(1);
    const surf = mk("polyline");
    surf.setAttribute("points", wave); surf.setAttribute("fill", "none");
    surf.setAttribute("stroke", "#3fa7ff"); surf.setAttribute("stroke-width", "1.8");
    dyn.appendChild(surf);
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
    // time-span labels across the x axis (3-minute window)
    label(x0, y1 + 14, "-3m", "start");
    label(x0 + w / 3, y1 + 14, "-2m", "middle");
    label(x0 + (2 * w) / 3, y1 + 14, "-1m", "middle");
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
    updateStatusPills();
  }
  // per-section LIVE/SIM pills: green LIVE only when that role has valid live NMEA, else amber SIM
  function setPill(id, live) {
    const p = $(id);
    if (!p) return;
    p.textContent = live ? "LIVE" : "SIM";
    p.className = "stat-pill " + (live ? "live" : "sim");
  }
  // AIS panel pill: 3-state (LIVE/SIM/OFF) off the AIS channel source. setPill only writes LIVE/SIM,
  // so the AIS pill needs its own setter to surface the OFF (no-AIS-channel) state.
  function setAisPill() {
    const p = $("pill-ais"); if (!p) return;
    const tag = parseSource(channelSourceByRole("ais")).tag;   // LIVE | SIM | OFF
    if (tag === "LIVE") { p.textContent = "LIVE"; p.className = "stat-pill live"; }
    else if (tag === "OFF") { p.textContent = "OFF"; p.className = "stat-pill off"; }
    else { p.textContent = "SIM"; p.className = "stat-pill sim"; }
  }
  function updateStatusPills() {
    const roleLive = (role) => parseSource(channelSourceByRole(role)).tag === "LIVE";
    setPill("pill-coords", roleLive("gps"));
    setPill("pill-heading", roleLive("heading"));
    setPill("pill-ship", roleLive("gps"));       // ship vectors are position/heading-derived
    setPill("pill-env", roleLive("instrument")); // wind + weather off the instrument channel
    setPill("pill-depth", roleLive("instrument")); // depth sounder (DBT) on the instrument channel
    setPill("pill-attitude", roleLive("heading")); // pitch/roll off the satcompass/heading source
    // engine, autopilot and derived alerts are display-only synthetic values → always SIM
    setPill("pill-prop", false);
    setPill("pill-autopilot", false);
    setPill("pill-alerts", false);
    // time is LIVE only when disciplined by a live NMEA source (not the SYSTEM clock / sim)
    const ts = String((lastHealth && lastHealth.time_source) || "").toUpperCase();
    setPill("pill-time", ts !== "" && ts !== "SYSTEM" && ts.indexOf("SIM") < 0 && ts !== "OFF");
    setAisPill();
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
    if (ch.port) hdr.appendChild(el("span", "com-port", "TX " + ch.port));
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
    // Real output toggle — a color-coded button (green = ON/emitting, red = OFF), not a checkbox,
    // so its state reads at a glance like Freeze/Clear.
    const btnOut = el("button", "small out-toggle", "");
    const frozenTag = el("span", "frozen-tag", "");
    const btnFreeze = el("button", "small", "Freeze view");
    const btnClear = el("button", "small", "Clear");
    ctl.appendChild(btnOut);
    ctl.appendChild(btnFreeze);
    ctl.appendChild(btnClear);
    ctl.appendChild(frozenTag);
    pane.appendChild(ctl);

    const state = {
      pane, feed, srcBadge, btnOut, frozen: false, count: 0, enabled: ch.enabled !== false,
      sAlive, sEmitVal, sErrVal, sSinks, frozenTag, btnFreeze,
    };

    function paintOut() {
      const on = state.enabled;
      btnOut.textContent = on ? "Output: ON" : "Output: OFF";
      btnOut.classList.toggle("out-on", on);
      btnOut.classList.toggle("out-off", !on);
    }
    state.paintOut = paintOut;
    paintOut();

    btnFreeze.addEventListener("click", () => {
      state.frozen = !state.frozen;
      btnFreeze.textContent = state.frozen ? "Resume view" : "Freeze view";
      feed.classList.toggle("frozen", state.frozen);
      frozenTag.textContent = state.frozen ? "VIEW FROZEN (log paused)" : "";
    });
    btnClear.addEventListener("click", () => { feed.textContent = ""; state.count = 0; });
    btnOut.addEventListener("click", async () => {
      const next = !state.enabled;
      btnOut.disabled = true;
      try { await control({ action: "channel", channel_id: ch.id, enabled: next }); state.enabled = next; paintOut(); }
      catch (e) { toast("Toggle failed: " + e.message); }
      finally { btnOut.disabled = false; }
    });

    panes[ch.id] = state;
    $("panes-out").appendChild(pane);
  }

  // Live INPUT pane (Streams tab, auto mode). Mirrors an output pane's freeze/clear semantics but
  // shows the raw RECEIVED feed for one configured input, with a compact single-line stats strip
  // (msgs/s, bus load, checksum-error rate + mini meter, talkers) and an inventory <details>.
  // Per-pane state lives in inPanes[inp.id] (NOT DOM getElementById on dynamic ids), exactly like
  // output panes use panes[ch.id]; pushLine reuses the {feed, frozen, count} shape verbatim.
  function buildInputPane(inp) {
    const fn = String(inp.function || "").toLowerCase();
    const pane = el("div", "pane pane-input");
    pane.setAttribute("data-input", inp.id);

    const hdr = el("div", "pane-hdr");
    const roleEl = el("div", "role");
    roleEl.appendChild(el("span", null, String(inp.id || "").toUpperCase()));
    if (inp.function) roleEl.appendChild(el("span", "talker", "  " + inp.function));
    hdr.appendChild(roleEl);
    if (inp.port) hdr.appendChild(el("span", "com-port", "RX " + inp.port));
    const dotEl = el("span", "dot " + (inp.live ? "live" : "dead"));
    hdr.appendChild(dotEl);
    pane.appendChild(hdr);

    const stats = el("div", "stats stats-compact");
    const aliveEl = el("span", "stat-alive" + (inp.live ? " up" : ""), inp.live ? "receiving" : "idle");
    const sMsgs = document.createElement("span"); sMsgs.innerHTML = 'msgs/s <b>—</b>';
    const msgsEl = sMsgs.querySelector("b");
    const sBus = document.createElement("span"); sBus.innerHTML = 'bus <b>—</b>%';
    const busEl = sBus.querySelector("b");
    const sErr = el("span", "errwrap"); sErr.innerHTML = 'err <b>—</b>%';
    const errEl = sErr.querySelector("b");
    const errMeter = el("div", "meter meter-mini");
    const errBar = document.createElement("span");
    errMeter.appendChild(errBar);
    sErr.appendChild(errMeter);
    const sTalkers = document.createElement("span"); sTalkers.innerHTML = 'talkers <b>—</b>';
    const talkersEl = sTalkers.querySelector("b");
    const invDetails = el("details", "inv-details");
    invDetails.appendChild(el("summary", null, "inventory"));
    const invTable = el("table", "kvtable");
    const invEl = document.createElement("tbody");
    invTable.appendChild(invEl);
    invDetails.appendChild(invTable);
    stats.appendChild(aliveEl);
    stats.appendChild(sMsgs);
    stats.appendChild(sBus);
    stats.appendChild(sErr);
    stats.appendChild(sTalkers);
    stats.appendChild(invDetails);
    pane.appendChild(stats);

    const feed = el("div", "feed");
    pane.appendChild(feed);

    const ctl = el("div", "pane-ctl");
    const btnFreeze = el("button", "small", "Freeze view");
    const btnClear = el("button", "small", "Clear");
    const frozenTag = el("span", "frozen-tag", "");
    ctl.appendChild(btnFreeze);
    ctl.appendChild(btnClear);
    ctl.appendChild(frozenTag);
    pane.appendChild(ctl);

    const state = {
      pane, feed, frozen: false, count: 0,
      dotEl, msgsEl, busEl, errEl, errBar, talkersEl, invEl, aliveEl, frozenTag, btnFreeze,
    };
    btnFreeze.addEventListener("click", () => {
      state.frozen = !state.frozen;
      btnFreeze.textContent = state.frozen ? "Resume view" : "Freeze view";
      feed.classList.toggle("frozen", state.frozen);
      frozenTag.textContent = state.frozen ? "VIEW FROZEN (log paused)" : "";
    });
    btnClear.addEventListener("click", () => { feed.textContent = ""; state.count = 0; });

    inPanes[inp.id] = state;
    $("panes-in").appendChild(pane);
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
    $("panes-out").appendChild(pane);
  }

  function pushLine(p, line) {
    if (!p || p.frozen) return;
    // Decide whether we're following the tail BEFORE mutating. Appending a line, trimming lines off
    // the top (removeChild), and the browser's scroll anchoring all shift scrollTop — so measuring
    // "am I at the bottom?" AFTER the mutation is unreliable and intermittently strands the view
    // mid-list (feed "stops scrolling" while data keeps arriving until you interact). CSS sets
    // overflow-anchor:none on .feed so the browser doesn't fight this manual re-pin.
    const atBottom = p.feed.scrollHeight - p.feed.scrollTop - p.feed.clientHeight < 40;
    const div = el("div", "line");
    div.appendChild(document.createTextNode(line));
    const crlf = el("span", "crlf", "␍␊");
    crlf.title = "\\r\\n (CR LF)";
    div.appendChild(crlf);
    p.feed.appendChild(div);
    p.count++;
    while (p.count > MAX_LINES && p.feed.firstChild) { p.feed.removeChild(p.feed.firstChild); p.count--; }
    if (atBottom) p.feed.scrollTop = p.feed.scrollHeight;
  }

  function appendLine(chId, line) {
    // Route to the channel's own pane (if it has one) AND the consolidated pane, which mirrors
    // the aggregate TCP tap by showing every channel's sentences merged, live.
    pushLine(panes[chId], line);
    pushLine(aggState, line);
  }

  // Route one RECEIVED input line to its live input pane (Streams tab). pushLine no-ops on an
  // undefined/frozen pane, so lines for an input without a pane (or while frozen) are dropped.
  function appendInputLine(inputId, line) {
    pushLine(inPanes[inputId], line);
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
      if (p.enabled !== enabled) { p.enabled = enabled; if (p.paintOut) p.paintOut(); }
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
    // a runtime mode change (simulate/replay -> auto) flips the Streams input section live
    updateInputSection();
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
    es.addEventListener("input_nmea", (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d && d.input != null) appendInputLine(d.input, d.line != null ? d.line : "");
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
      else if (activeTab === "config") { renderRouteProgress(d.route || null); applyDrivenFields(d.driven_fields); }
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
  // A2: value editors regrouped into 8 cards mirroring the Conning section names. Each entry is a
  // slug -> [[wire_key, label], ...] list of wire-backed (data-field, action=update) inputs. The
  // container ids are "grp-<slug>" (owned by index.html); sea_state renders as a <select>. Cards
  // that only carry non-data-field controls (time-source, overrides, depth-sim toggle) have their
  // special inputs authored in index.html and are read/written directly by the handlers below.
  // Hemisphere-entered fields: config stores SIGNED decimal degrees (south/west negative), which
  // meant typing "-122.47" for a western longitude. These render as magnitude + hemisphere select.
  // Sign convention deliberately matches fmtLat/fmtLon on the display side, so entry and readout
  // cannot drift apart. The magnitude input KEEPS the canonical "cfg-<key>" id and data-field so
  // driven-field greying (applyDrivenFields) and allCfgFields() keep working untouched.
  const HEMI = { lat: ["N", "S"], lon: ["E", "W"] };  // [positive, negative]

  const CFG_FIELDS = {
    "coordinates": [["lat", "Latitude"], ["lon", "Longitude"]],
    "heading-motion": [
      ["heading_true_deg", "Heading T (°)"], ["heading_mag_deg", "Heading M (°)"], ["mag_variation_deg", "Variation (°)"],
      ["sog_kn", "SOG (kn)"], ["cog_deg", "COG (°)"], ["rot_dpm", "ROT (°/min)"], ["stw_kn", "STW (kn)"],
    ],
    "environment": [["sea_state", "Sea state (0–9)"], ["wind_speed_kn", "Wind spd (kn)"], ["wind_dir_deg", "Wind dir (°)"]],
    "depth": [["depth_m", "Depth (m)"]],
    "ship-helm": [["rudder_angle_deg", "Rudder (°)"]],
    "current": [["set_deg", "Set (°)"], ["drift_kn", "Drift (kn)"]],
    "gnss": [["altitude_m", "Altitude (m)"], ["fix_quality", "Fix quality"], ["satellites", "Satellites"], ["hdop", "HDOP"]],
    "time": [],
  };
  function buildConfigForms() {
    const mkNumber = (key, label) => {
      const f = el("div", "field");
      f.appendChild(el("label", null, label));
      const inp = document.createElement("input");
      inp.type = "number"; inp.step = "any"; inp.id = "cfg-" + key; inp.dataset.field = key;
      f.appendChild(inp);
      return f;
    };
    const mkCoord = (key, label) => {
      const [pos, neg] = HEMI[key];
      const f = el("div", "field");
      f.appendChild(el("label", null, label));
      const row = el("div", "coord-entry");
      const inp = document.createElement("input");
      // min=0: the magnitude is unsigned. A negative magnitude with S/W selected would otherwise
      // multiply to a POSITIVE value that passes RANGES silently (-122.47 with W -> +122.47).
      inp.type = "number"; inp.step = "any"; inp.min = "0";
      inp.id = "cfg-" + key; inp.dataset.field = key;
      const sel = document.createElement("select");
      sel.id = "cfg-" + key + "-hemi";
      for (const h of [pos, neg]) { const o = document.createElement("option"); o.value = h; o.textContent = h; sel.appendChild(o); }
      row.appendChild(inp); row.appendChild(sel);
      f.appendChild(row);
      return f;
    };
    const mkSeaState = () => {
      const f = el("div", "field");
      f.appendChild(el("label", null, "Sea state (0–9)"));
      const sel = document.createElement("select"); sel.id = "cfg-sea_state"; sel.dataset.field = "sea_state";
      sel.style.width = "200px";
      SEA_STATES.forEach((lbl, i) => { const o = document.createElement("option"); o.value = String(i); o.textContent = lbl; sel.appendChild(o); });
      f.appendChild(sel);
      return f;
    };
    for (const slug of Object.keys(CFG_FIELDS)) {
      const c = $("grp-" + slug);
      if (!c) continue;              // index.html owns the card scaffolding; skip a missing container
      c.textContent = "";
      for (const [key, label] of CFG_FIELDS[slug]) {
        c.appendChild(key === "sea_state" ? mkSeaState() : HEMI[key] ? mkCoord(key, label) : mkNumber(key, label));
      }
    }
    bindConfigCards();
  }

  function allCfgInputs() {
    return Array.from(document.querySelectorAll("[data-field]"));
  }
  function readCfgField(key) {
    const node = $("cfg-" + key);
    if (!node) return null;
    const raw = String(node.value).trim();
    if (raw === "") return null;
    const mag = Number(raw);
    if (!HEMI[key]) return { node, raw, n: mag };
    // Magnitude + hemisphere -> signed decimal degrees, the only form the server accepts.
    const sel = $("cfg-" + key + "-hemi");
    const neg = sel && sel.value === HEMI[key][1];
    return { node, raw, n: neg ? -Math.abs(mag) : mag, magnitude: mag, hemi: sel ? sel.value : HEMI[key][0] };
  }
  function validateCfgField(key) {
    const v = readCfgField(key);
    const node = $("cfg-" + key);
    if (!v) { if (node) node.classList.remove("invalid"); return { present: false }; }
    if (!Number.isFinite(v.n)) { node.classList.add("invalid"); return { present: true, ok: false, msg: key + " is not a number" }; }
    if (HEMI[key] && v.magnitude < 0) {
      // RANGES cannot catch this: -122.47 with W selected combines to +122.47, which is in range
      // but the OPPOSITE hemisphere from what the operator sees selected.
      node.classList.add("invalid");
      return { present: true, ok: false, msg: key + ": enter a positive value and pick " + HEMI[key].join("/") };
    }
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
      // /api/ports is the adapter enumeration: opaque handles + kernel name + what each is
      // receiving. Never a device path -- the server maps handle -> path on save (R19).
      const [inRes, portRes] = await Promise.all([fetch("/api/inputs"), fetch("/api/ports")]);
      const inputs = await inRes.json();
      let ports = [];
      try { ports = await portRes.json(); } catch (e) { ports = []; }
      renderConfigInputs(Array.isArray(inputs) ? inputs : [], Array.isArray(ports) ? ports : []);
    } catch (e) { /* leave prior render */ }
  }
  function renderConfigInputs(inputs, ports) {
    ports = ports || [];
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

      // WHICH physical adapter this slot reads. Options are opaque handles labelled with the
      // kernel port and what the adapter is currently receiving -- that is how you tell them
      // apart, since the by-id name (brand + serial) is deliberately never sent (R19).
      const psel = document.createElement("select");
      psel.id = "cfg-inport-" + inp.id; psel.dataset.slotPort = inp.id;
      const keep = document.createElement("option");
      keep.value = ""; keep.textContent = ports.length ? "— leave unchanged —" : "— no adapters detected —";
      psel.appendChild(keep);
      for (const pt of ports) {
        const o = document.createElement("option");
        o.value = pt.handle;
        const bits = [pt.port || "unknown port"];
        if (pt.detected_class) bits.push("seeing " + pt.detected_class);
        else bits.push(pt.live ? "live" : "idle");
        if (pt.assigned_to && pt.assigned_to !== inp.id) bits.push("in use by " + pt.assigned_to);
        o.textContent = bits.join(" · ");
        if (pt.assigned_to === inp.id) o.selected = true;
        psel.appendChild(o);
      }
      wrap.appendChild(psel);

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
      if (!node || s[key] == null) continue;
      if (HEMI[key]) {
        // Live state is signed; the widget shows magnitude + hemisphere, so split rather than
        // dumping "-122.47" into a min=0 magnitude box with the select left stale.
        const val = Number(s[key]);
        node.value = Math.abs(val);
        const sel = $("cfg-" + key + "-hemi");
        if (sel) sel.value = HEMI[key][val < 0 ? 1 : 0];
      } else {
        node.value = s[key];
      }
    }
    const sea = $("cfg-sea_state");
    if (sea && s.sea_state != null) sea.value = String(Math.max(0, Math.min(9, Math.round(Number(s.sea_state)))));
  }

  // Per-card status line (A2). Writes to "cfg-msg-<slug>"; falls back to a toast if absent.
  function setCardMsg(slug, t, c) {
    const m = $("cfg-msg-" + slug);
    if (m) { m.textContent = t || ""; m.className = "msg" + (c ? " " + c : ""); }
    else if (t) toast(t);
  }

  // A2: Apply (live) for one section -> POST /api/control {action:"update", <that card's data-field
  // inputs>}. Reuses validateCfgField; sea_state comes off its <select>. The Time card has no
  // wire-backed fields (time-source applies on next Start), so its Apply just informs.
  async function cardApply(slug) {
    if (slug === "time") { setCardMsg(slug, "Time source applies on next Start — use Save as defaults.", ""); return; }
    const fields = (CFG_FIELDS[slug] || []).map((f) => f[0]);
    const body = { action: "update" };
    let any = false;
    for (const key of fields) {
      if (key === "sea_state") {
        const sea = $("cfg-sea_state");
        if (sea && sea.value !== "") { body.sea_state = Number(sea.value); any = true; }
        continue;
      }
      const v = validateCfgField(key);
      if (v.present && v.ok === false) { setCardMsg(slug, v.msg, "err"); return; }
      if (v.present && v.ok) { body[key] = v.value; any = true; }
    }
    if (!any) { setCardMsg(slug, "Enter at least one field to apply.", "err"); return; }
    try { await control(body); setCardMsg(slug, "Applied to running engine.", "ok"); }
    catch (e) { setCardMsg(slug, "Apply failed: " + e.message, "err"); }
  }

  // A2/A4/A5/A6: Save as defaults for one section -> POST /api/config/initial-state with only that
  // card's fields (partial, skip-on-empty). Env/Ship-Helm also fold in display_overrides (A4); Time
  // adds the time-source fields (A5); Depth adds the depth_sim toggle (A6). Every wire key is guarded
  // by the INITIAL_FIELDS allow-list so a stray field can never be posted.
  async function cardSave(slug) {
    const fields = (CFG_FIELDS[slug] || []).map((f) => f[0]);
    const body = {};
    let any = false;
    for (const key of fields) {
      if (INITIAL_FIELDS.indexOf(key) < 0) continue;
      if (key === "sea_state") {
        const sea = $("cfg-sea_state");
        if (sea && sea.value !== "") { body.sea_state = Number(sea.value); any = true; }
        continue;
      }
      const v = validateCfgField(key);
      if (v.present && v.ok === false) { setCardMsg(slug, v.msg, "err"); return; }
      if (v.present && v.ok) { body[key] = v.value; any = true; }
    }
    // A4: display overrides ride the Environment + Ship/Helm cards (display-only — not on NMEA).
    // Save MIRRORS the override boxes: a filled box persists its value, a blank box sends null so a
    // cleared override is actually removed from the saved config (not resurrected on next restart).
    if (slug === "environment" || slug === "ship-helm") {
      const ov = overridesForSave();
      if (ov === null) { setCardMsg(slug, "Override values must be numeric.", "err"); return; }
      if (Object.keys(ov).length) { body.display_overrides = ov; any = true; }
    }
    // A5: time-source persist (mode / epoch / rate) on the Time card.
    if (slug === "time") {
      const modeSel = $("cfg-time_source_mode");
      if (modeSel && modeSel.value) { body.time_source_mode = modeSel.value; any = true; }
      const ep = $("cfg-time_source_epoch");
      if (ep && String(ep.value).trim() !== "") { body.time_source_epoch = String(ep.value).trim(); any = true; }
      const rt = $("cfg-time_source_rate");
      if (rt && String(rt.value).trim() !== "") { const n = Number(rt.value); if (Number.isFinite(n)) { body.time_source_rate = n; any = true; } }
    }
    // A6: depth-sim enable toggle -> {enabled} block; server defaults fill the rest.
    if (slug === "depth") {
      const en = $("cfg-depth_sim-enabled");
      if (en) { body.depth_sim = { enabled: !!en.checked }; any = true; }
    }
    // Rudder-hold sim toggle rides the Ship/Helm card -> {enabled} block; server defaults fill the rest.
    if (slug === "ship-helm") {
      const en = $("cfg-rudder_sim-enabled");
      if (en) { body.rudder_sim = { enabled: !!en.checked }; any = true; }
    }
    // Heading-hold sim toggle rides the Heading & Motion card -> {enabled} block; server defaults fill the rest.
    if (slug === "heading-motion") {
      const en = $("cfg-heading_sim-enabled");
      if (en) { body.heading_sim = { enabled: !!en.checked }; any = true; }
    }
    // Wind sim toggle rides the Environment card -> {enabled} block; server defaults fill the rest.
    if (slug === "environment") {
      const en = $("cfg-wind_sim-enabled");
      if (en) { body.wind_sim = { enabled: !!en.checked }; any = true; }
    }
    if (!any) { setCardMsg(slug, "Nothing to save in this section.", "err"); return; }
    const r = await postJson("/api/config/initial-state", body);
    if (r.ok) setCardMsg(slug, "Saved as defaults (applies on next Start).", "ok");
    else setCardMsg(slug, "Save failed: " + ((r.data && r.data.detail) || ("HTTP " + r.status)), "err");
  }

  // A4: collect the display-override inputs (data-override; cosmetic, NOT data-field). Only present,
  // finite values are returned so a blank input leaves that key on auto.
  function collectOverrides() {
    const out = {};
    for (const node of document.querySelectorAll("[data-override]")) {
      const raw = String(node.value).trim();
      if (raw === "") continue;
      const n = Number(raw);
      if (Number.isFinite(n)) out[node.dataset.override] = n;
    }
    return out;
  }
  // A4 (persist): mirror the override boxes for Save-as-defaults — filled box -> value, blank box ->
  // null (removes that key from the saved config). Returns null if any box holds a non-numeric value.
  function overridesForSave() {
    const out = {};
    for (const node of document.querySelectorAll("[data-override]")) {
      const raw = String(node.value).trim();
      if (raw === "") { out[node.dataset.override] = null; continue; }
      const n = Number(raw);
      if (!Number.isFinite(n)) return null;
      out[node.dataset.override] = n;
    }
    return out;
  }
  function overrideMsg(t, c) { setCardMsg("environment", t, c); setCardMsg("ship-helm", t, c); }
  async function overrideApply() {
    const overrides = collectOverrides();
    if (!Object.keys(overrides).length) { overrideMsg("Enter at least one display value to override.", "err"); return; }
    try { await control({ action: "display_override", overrides }); overrideMsg("Display overrides applied (display-only — not on NMEA).", "ok"); }
    catch (e) { overrideMsg("Override failed: " + e.message, "err"); }
  }
  async function overrideClear() {
    try { await control({ action: "display_override", clear: true }); overrideMsg("Display overrides cleared (back to auto).", "ok"); }
    catch (e) { overrideMsg("Clear failed: " + e.message, "err"); }
  }

  // A3b: grey out inputs the engine overwrites each tick (route -> cog/sog, auto-RX -> rx_accept,
  // depth-sim -> depth_m). Reads state.driven_fields from /api/state and every SSE state frame.
  function applyDrivenFields(list) {
    const driven = new Set(Array.isArray(list) ? list : []);
    for (const node of allCfgInputs()) {
      const on = driven.has(node.dataset.field);
      node.disabled = on;
      node.classList.toggle("driven", on);
      if (on) node.title = "Driven by route / auto input / depth-heading-rudder sim — manual edits won't stick";
      else node.removeAttribute("title");
    }
  }

  // A6: client-side shallow-water alert threshold persisted to localStorage, overriding ALERT_DEPTH_M.
  function onDepthAlertChange() {
    const da = $("cfg-depth-alert");
    if (!da) return;
    const raw = String(da.value).trim();
    const n = Number(raw);
    if (raw === "" || !Number.isFinite(n) || n <= 0) return;
    ALERT_DEPTH_M = n;
    try { localStorage.setItem("mb.alertDepthM", String(n)); } catch (e) {}
    const lbl = $("depth-alert"); if (lbl) lbl.textContent = ALERT_DEPTH_M.toFixed(1);
    renderDepthGraph(); renderAlerts();
  }

  // Conning temp unit toggle (client-side, display-only, persisted). Flips the unit-span text and
  // reflows the last frame's numbers immediately. Modeled on onDepthAlertChange.
  function onTempUnitChange() {
    const sel = $("cfg-temp-unit"); if (!sel) return;
    TEMP_UNIT = (sel.value === "F") ? "F" : "C";
    try { localStorage.setItem("mb.tempUnit", TEMP_UNIT); } catch (e) {}
    const u = TEMP_UNIT === "F" ? "°F" : "°C";
    const wu = $("env-wtemp-unit"); if (wu) wu.textContent = u;
    const au = $("env-atemp-unit"); if (au) au.textContent = u;
    if (lastState) requestConningPaint();   // reflow numbers from the last frame
  }

  // Load persisted A4/A5/A6 values into their (index.html-owned) inputs.
  function loadTimeSourceIntoConfig() {
    const ts = (cfg && cfg.time_source) || {};
    const modeSel = $("cfg-time_source_mode"); if (modeSel && ts.mode) modeSel.value = ts.mode;
    const ep = $("cfg-time_source_epoch"); if (ep) ep.value = ts.epoch || "";
    const rt = $("cfg-time_source_rate"); if (rt && ts.rate != null) rt.value = ts.rate;
  }
  function loadDisplayOverridesIntoConfig() {
    const ov = (cfg && cfg.display_overrides) || {};
    for (const node of document.querySelectorAll("[data-override]")) {
      const key = node.dataset.override;
      node.value = (ov && ov[key] != null) ? ov[key] : "";
    }
  }
  // Mirror the server-side effective_*_sim helpers: the background sims default ON in simulate mode
  // when their config block is ABSENT (the default-on lives in the effective helper, not config.json),
  // so an unsaved/absent block must render as CHECKED. An explicit block uses its own enabled flag;
  // outside simulate mode the sims are inert, so the box reads unchecked.
  function effectiveSimEnabled(block) {
    const simulate = String((cfg && cfg.mode) || "").toLowerCase() === "simulate";
    if (!simulate) return false;
    if (block == null) return true;         // absent → default-on
    return !!block.enabled;                 // explicit → honour it
  }
  function loadDepthSimIntoConfig() {
    const en = $("cfg-depth_sim-enabled");
    if (en) en.checked = effectiveSimEnabled(cfg && cfg.depth_sim);
    const da = $("cfg-depth-alert"); if (da && String(da.value).trim() === "") da.value = ALERT_DEPTH_M.toFixed(1);
    loadSteeringSimIntoConfig();
  }
  // Load the rudder-hold / heading-hold sim enable toggles from cfg.rudder_sim / cfg.heading_sim.
  function loadSteeringSimIntoConfig() {
    const re = $("cfg-rudder_sim-enabled");
    if (re) re.checked = effectiveSimEnabled(cfg && cfg.rudder_sim);
    const he = $("cfg-heading_sim-enabled");
    if (he) he.checked = effectiveSimEnabled(cfg && cfg.heading_sim);
    const we = $("cfg-wind_sim-enabled");
    if (we) we.checked = effectiveSimEnabled(cfg && cfg.wind_sim);
  }

  // Legacy config-level Save (mode / channels / inputs / route / replay / AIS). Not part of the A2
  // value-editor cards; preserved and bound only if a "cfg-save" trigger still exists in the DOM.
  async function saveConfigLevel() {
    const body = {};
    const modeR = document.querySelector('input[name="cfg-mode"]:checked');
    if (modeR) body.mode = modeR.value;
    const emitMap = collectEmitOverrides();
    body.channels = channelOrder.map((id) => {
      const cb = $("cfg-ch-" + id);
      const entry = { id, enabled: cb ? cb.checked : true };
      const em = emitMap[id];
      if (em && em.length) entry.emit = em;
      return entry;
    });
    const inputSels = Array.from(document.querySelectorAll("[data-slot]"));
    if (inputSels.length) {
      body.inputs = inputSels.map((s) => {
        const entry = { id: s.dataset.slot, function: s.value };
        // Empty handle => "leave the current binding alone", so a function-only save is unchanged.
        const pv = $("cfg-inport-" + s.dataset.slot);
        if (pv && pv.value) entry.handle = pv.value;
        return entry;
      });
    }
    const routeEnabled = !!($("cfg-route-enabled") && $("cfg-route-enabled").checked);
    const wpts = parseWaypoints();
    if (routeEnabled || wpts.length) {
      const spRaw = String(($("cfg-route-speed") || {}).value || "").trim();
      body.route = {
        enabled: routeEnabled, waypoints: wpts,
        speed_kn: spRaw === "" ? 0 : Number(spRaw),
        loop: !!($("cfg-route-loop") && $("cfg-route-loop").checked),
      };
    }
    const replayFileNode = $("cfg-replay-file");
    const replayFile = replayFileNode ? String(replayFileNode.value).trim() : "";
    if (body.mode === "replay" || replayFile) {
      const spRaw = String(($("cfg-replay-speed") || {}).value || "").trim();
      body.replay = {
        enabled: body.mode === "replay", file: replayFile,
        loop: !!($("cfg-replay-loop") && $("cfg-replay-loop").checked),
        speed: spRaw === "" ? 1.0 : Number(spRaw),
        scope: ($("cfg-replay-scope") || {}).value || "full",
      };
    }
    const aisCh = (cfg && Array.isArray(cfg.channels))
      ? cfg.channels.find((c) => String(c.role || "").toLowerCase() === "ais") : null;
    if (aisCh) {
      const aisEnabled = $("cfg-ais-enabled");
      if (aisEnabled) {
        const at = { enabled: aisEnabled.checked };
        const profile = ($("cfg-ais-profile") || {}).value;
        if (profile) at.profile_path = profile;
        const countRaw = String(($("cfg-ais-count") || {}).value || "").trim();
        if (countRaw !== "" && Number.isFinite(Number(countRaw))) at.target_count = Number(countRaw);
        body.ais_traffic = at;
      }
    }
    const r = await postJson("/api/config/initial-state", body);
    if (r.ok) setCfgMsg("Saved mode / channels / route / replay / AIS defaults.", "ok");
    else setCfgMsg("Save failed: " + ((r.data && r.data.detail) || ("HTTP " + r.status)), "err");
  }

  // Wire the per-card + override + depth-alert + legacy triggers once. Every lookup is guarded so a
  // card the index.html author has not (yet) placed simply stays unbound instead of throwing at load.
  let _cardsBound = false;
  function bindConfigCards() {
    if (_cardsBound) return;
    _cardsBound = true;
    for (const slug of Object.keys(CFG_FIELDS)) {
      const ap = $("cfg-apply-" + slug); if (ap) ap.addEventListener("click", () => cardApply(slug));
      const sv = $("cfg-save-" + slug); if (sv) sv.addEventListener("click", () => cardSave(slug));
    }
    const ovA = $("cfg-override-apply"); if (ovA) ovA.addEventListener("click", overrideApply);
    const ovC = $("cfg-override-clear"); if (ovC) ovC.addEventListener("click", overrideClear);
    // second Apply/Clear pair on the Ship / Helm card (fuel + engine-order overrides live there too)
    const ovAs = $("cfg-override-apply-ship"); if (ovAs) ovAs.addEventListener("click", overrideApply);
    const ovCs = $("cfg-override-clear-ship"); if (ovCs) ovCs.addEventListener("click", overrideClear);
    const da = $("cfg-depth-alert"); if (da) da.addEventListener("change", onDepthAlertChange);
    const tu = $("cfg-temp-unit");
    if (tu) {
      tu.value = TEMP_UNIT;
      const u = TEMP_UNIT === "F" ? "°F" : "°C";
      const wu = $("env-wtemp-unit"); if (wu) wu.textContent = u;
      const au = $("env-atemp-unit"); if (au) au.textContent = u;
      tu.addEventListener("change", onTempUnitChange);
    }
    const cl = $("cfg-load"); if (cl) cl.addEventListener("click", loadConfigCurrent);
    const cs = $("cfg-save"); if (cs) cs.addEventListener("click", saveConfigLevel);
  }

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
      loadTimeSourceIntoConfig();            // A5
      loadDisplayOverridesIntoConfig();      // A4
      loadDepthSimIntoConfig();              // A6
      applyDrivenFields(s && s.driven_fields); // A3b: grey out engine-driven inputs
      await loadAisTrafficIntoConfig();
      await refreshInputs();
      setCfgMsg((s && s.running !== false) ? "Loaded current state + config." : "Loaded config (engine stopped — no live state).", "ok");
    } catch (e) { setCfgMsg("Load failed: " + e.message, "err"); }
  }
  function setCfgMsg(t, c) { const m = $("cfg-msg"); if (m) { m.textContent = t || ""; m.className = "msg" + (c ? " " + c : ""); } else if (t) toast(t); }

  /* =====================================================================
   *  MAINTENANCE TAB (poll /api/diag while active)
   * ===================================================================== */
  function startDiagPoll() { if (diagTimer) return; pollDiag(); diagTimer = setInterval(pollDiag, 2000); }
  function stopDiagPoll() { if (diagTimer) { clearInterval(diagTimer); diagTimer = null; } }
  // Live-mode gate for the Streams INPUTS section: inputs stream ONLY in auto mode. Read the LIVE
  // mode off the health frame (never the boot-frozen cfg.mode) so a runtime mode change flips it.
  function isAutoMode() {
    return String((lastHealth && lastHealth.mode) || "").toLowerCase() === "auto";
  }
  // Show the INPUTS grid only in auto mode with built panes; otherwise collapse to the empty-state
  // note (CSS-driven via .no-inputs — no direct JS style). Also keeps the Streams diag poll in step
  // with a live mode change while the tab is open.
  function updateInputSection() {
    const sec = $("streams-in-section");
    if (!sec) return;
    const show = isAutoMode() && Object.keys(inPanes).length > 0;
    sec.classList.toggle("no-inputs", !show);
    if (activeTab === "streams") {
      if (show && !diagTimer) startDiagPoll();
      else if (!show && diagTimer) stopDiagPoll();
    }
  }
  async function pollDiag() {
    let d;
    try { const res = await fetch("/api/diag"); d = await res.json(); } catch (e) { return; }
    if (activeTab === "maintenance") renderDiag(d);
    else if (activeTab === "streams") renderInputStats(d);
  }
  // Drive the compact per-input stats strips on the Streams tab from the same /api/diag payload
  // the Maintenance tab uses (dispatched by active tab in pollDiag). One diag port -> one input pane.
  function renderInputStats(d) {
    const ports = (d && Array.isArray(d.ports)) ? d.ports : [];
    for (const p of ports) {
      const s = inPanes[p.port_id];
      if (!s) continue;
      // Liveness must track the live feed, not the frozen page-load /api/inputs value: a port is
      // "receiving" when sentences are currently arriving. /api/diag carries no live flag, so derive
      // it from the rolling rate (verdict "no-data" or zero rate => idle).
      const up = String(p.verdict || "") !== "no-data" && (Number(p.sentences_per_s) || 0) > 0;
      s.dotEl.className = "dot " + (up ? "live" : "dead");
      s.aliveEl.className = "stat-alive" + (up ? " up" : "");
      s.aliveEl.textContent = up ? "receiving" : "idle";
      const structured = (Number(p.valid) || 0) + (Number(p.bad_checksum) || 0);
      const errRate = structured ? (Number(p.bad_checksum) || 0) / structured : 0;
      s.msgsEl.textContent = num(p.sentences_per_s, 2);
      s.busEl.textContent = num(p.bus_load_pct, 1);
      s.errEl.textContent = (errRate * 100).toFixed(1);
      s.errBar.style.width = Math.min(100, errRate * 100).toFixed(1) + "%";
      s.errBar.style.background = errRate > 0.2 ? "var(--down)" : (errRate > 0.02 ? "var(--warn)" : "var(--ok)");
      const tk = (Array.isArray(p.talkers) && p.talkers.length) ? p.talkers.join(", ") : "—";
      s.talkersEl.textContent = tk;
      // inventory rows (formatter · rate Hz · last seen s) into s.invEl
      s.invEl.textContent = "";
      const inv = p.inventory || {};
      for (const k of Object.keys(inv)) {
        const info = inv[k] || {};
        const tr = document.createElement("tr");
        tr.appendChild(el("td", null, k));
        tr.appendChild(el("td", null, num(info.rate_hz, 2) + " Hz · " + (info.last_seen_s == null ? "—" : num(info.last_seen_s, 1) + "s")));
        s.invEl.appendChild(tr);
      }
    }
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
    const banner = $("sec-default-banner");
    if (banner) banner.style.display = d.password_is_default ? "" : "none";
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

  // --- Change-web-password card + first-login banner (wired ONCE at init, not per poll) ----------
  // kind: "err" (red) | "ok" (green) | "" (muted). Reuses the shared .msg/.msg.err/.msg.ok palette
  // so a real error POPS in red instead of blending into the muted hint above it.
  function setPwStatus(t, kind) {
    const s = $("sec-pw-status");
    if (s) { s.textContent = t; s.className = "msg" + (kind ? " " + kind : ""); }
  }

  // After a change, caddy bounces and drops the connection. Poll /api/security until it answers
  // again (the browser re-auths with the new password), then replace the transient "changing"
  // message with a clean confirmation and reconcile the first-login banner.
  async function reconnectAfterPwChange() {
    const deadline = Date.now() + 45000;
    const wait = (ms) => new Promise((r) => setTimeout(r, ms));
    while (Date.now() < deadline) {
      await wait(2000);
      try {
        const resp = await fetch("/api/security", { cache: "no-store" });
        if (!resp.ok) continue;              // caddy up but old creds cached => browser will re-prompt
        const d = await resp.json();
        setPwStatus("Password changed successfully.", "ok");
        const banner = $("sec-default-banner");
        if (banner) banner.style.display = d.password_is_default ? "" : "none";
        return;
      } catch (e) { /* caddy still restarting — keep polling */ }
    }
  }

  async function submitPwChange() {
    const cur = $("sec-pw-current").value, pw = $("sec-pw-new").value, cf = $("sec-pw-confirm").value;
    if (!cur)           { setPwStatus("Enter your current password.", "err"); return; }
    if (pw.length < 12) { setPwStatus("New password must be at least 12 characters.", "err"); return; }
    if (pw !== cf)      { setPwStatus("New passwords do not match.", "err"); return; }
    setPwStatus("Applying…", "");
    const CHANGING = "Password changing — you'll be asked to sign in again with the new password in a few seconds.";
    let changing = false;
    try {
      const r = await postJson("/api/security/rotate-password", { current_password: cur, new_password: pw });
      if (r.status === 400 || (r.data && r.data.status === "failure")) {
        // Wrong current password / policy / rollback — surface it in RED and keep the fields.
        setPwStatus((r.data && r.data.detail) || "Change failed.", "err");
        return;
      }
      changing = true;                       // ok OR pending: caddy is bouncing; browser will re-prompt
      setPwStatus(CHANGING, "");
    } catch (e) {
      changing = true;                       // dropped connection by the caddy restart == success path
      setPwStatus(CHANGING, "");
    } finally {
      if (changing) {                        // only wipe plaintext once we've actually submitted a change
        $("sec-pw-current").value = ""; $("sec-pw-new").value = ""; $("sec-pw-confirm").value = "";
      }
    }
    if (changing) reconnectAfterPwChange();
  }

  {
    const bChange = $("sec-banner-change");
    if (bChange) bChange.addEventListener("click", () => {
      $("sec-pw-card").scrollIntoView({ behavior: "smooth" });
      $("sec-pw-new").focus();
    });
    const bDismiss = $("sec-banner-dismiss");
    if (bDismiss) bDismiss.addEventListener("click", async () => {
      try {
        const r = await postJson("/api/security/dismiss-default-prompt", {});
        if (r.data && r.data.status === "ok") {
          const banner = $("sec-default-banner"); if (banner) banner.style.display = "none";
          pollSecurity();
        }
      } catch (e) { /* leave banner in place on failure */ }
    });
    const submit = $("sec-pw-submit");
    if (submit) submit.addEventListener("click", submitPwChange);
  }

  /* =====================================================================
   *  BOOTSTRAP
   * ===================================================================== */
  async function init() {
    try {
      const res = await fetch("/api/config");
      cfg = await res.json();
    } catch (e) {
      $("panes-out").appendChild(el("div", null, "Failed to load config: " + e.message));
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
    // Input panes are built regardless of mode (/api/inputs lists configured slots in any mode);
    // the section's .no-inputs class (via updateInputSection) governs grid-vs-empty-state display.
    try {
      const ir = await fetch("/api/inputs");
      const inputs = await ir.json();
      for (const inp of (Array.isArray(inputs) ? inputs : [])) buildInputPane(inp);
    } catch (e) { /* leave inputs empty -> empty-state */ }
    updateInputSection();
    buildConfigForms();
    buildConfigChannels();
    buildConfigSentences();
    loadRouteReplayIntoConfig();
    loadTimeSourceIntoConfig();            // A5
    loadDisplayOverridesIntoConfig();      // A4
    loadDepthSimIntoConfig();              // A6
    await loadAisTrafficIntoConfig();
    setMode(cfg.mode || "simulate");
    connectStream();
  }

  init();
})();
