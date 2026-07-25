"use strict";
(function () {
  const MAX_LINES = 300;
  const STALE_MS = 3500;

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
  // build compass rose ticks + wind rose ticks once
  (function buildCompassCard() {
    const card = $("compass-card");
    const cx = 100, cy = 100;
    for (let deg = 0; deg < 360; deg += 10) {
      const major = deg % 30 === 0;
      const r1 = 94, r2 = major ? 80 : 87;
      const a = (deg - 90) * Math.PI / 180;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", (cx + r1 * Math.cos(a)).toFixed(1));
      line.setAttribute("y1", (cy + r1 * Math.sin(a)).toFixed(1));
      line.setAttribute("x2", (cx + r2 * Math.cos(a)).toFixed(1));
      line.setAttribute("y2", (cy + r2 * Math.sin(a)).toFixed(1));
      line.setAttribute("stroke", major ? "#8b949e" : "#3d444d");
      line.setAttribute("stroke-width", major ? "1.5" : "1");
      card.appendChild(line);
      if (major) {
        const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
        const rr = 70;
        t.setAttribute("x", (cx + rr * Math.cos(a)).toFixed(1));
        t.setAttribute("y", (cy + rr * Math.sin(a) + 4).toFixed(1));
        t.setAttribute("fill", "#c9d1d9");
        t.setAttribute("font-size", "10");
        t.setAttribute("font-family", "monospace");
        t.setAttribute("text-anchor", "middle");
        const cards = { 0: "N", 90: "E", 180: "S", 270: "W" };
        t.textContent = cards[deg] || String(deg / 10 * 10).padStart(2, "0").slice(0, 2);
        if (!cards[deg]) t.textContent = (deg < 100 ? "0" : "") + deg;
        card.appendChild(t);
      }
    }
  })();
  (function buildWindTicks() {
    const g = $("wind-ticks"), cx = 100, cy = 100;
    for (let deg = 0; deg < 360; deg += 30) {
      const a = (deg - 90) * Math.PI / 180;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", (cx + 94 * Math.cos(a)).toFixed(1));
      line.setAttribute("y1", (cy + 94 * Math.sin(a)).toFixed(1));
      line.setAttribute("x2", (cx + 84 * Math.cos(a)).toFixed(1));
      line.setAttribute("y2", (cy + 84 * Math.sin(a)).toFixed(1));
      line.setAttribute("stroke", "#3d444d");
      g.appendChild(line);
    }
  })();

  // Digital readout registry: id -> {label, unit, srcKind, get(state)}
  // srcKind: "gps" | "heading" | "sim" | "time"
  const READOUTS = [
    { key: "sog", label: "SOG", unit: "kn", srcKind: "gps", get: (s) => num(s.sog_kn, 1) },
    { key: "stw", label: "STW", unit: "kn", srcKind: "sim", get: (s) => num(s.stw_kn, 1) },
    { key: "cog", label: "COG", unit: "°", srcKind: "gps", get: (s) => num(s.cog_deg, 0) },
    { key: "hdg", label: "HDG", unit: "T / M", srcKind: "heading", get: (s) => num(s.heading_true_deg, 0) + " / " + num(s.heading_mag_deg, 0) },
    { key: "lat", label: "Latitude", unit: "", srcKind: "gps", get: (s) => fmtLat(s.lat) },
    { key: "lon", label: "Longitude", unit: "", srcKind: "gps", get: (s) => fmtLon(s.lon) },
    { key: "depth", label: "Depth", unit: "m", srcKind: "sim", get: (s) => num(s.depth_m, 1) },
    { key: "utc", label: "UTC", unit: "", srcKind: "time", get: (s) => fmtUtc(s.utc) },
    { key: "sea", label: "Sea State", unit: "WMO", srcKind: "sim", get: (s) => num(s.sea_state, 0) },
  ];
  const roNodes = {};
  (function buildReadouts() {
    const wrap = $("readouts");
    for (const r of READOUTS) {
      const box = el("div", "ro");
      const top = el("div", "ro-top");
      top.appendChild(el("span", "ro-label", r.label));
      const tag = el("span", "src src-off", "OFF");
      top.appendChild(tag);
      box.appendChild(top);
      const val = el("div", "ro-val", "---");
      box.appendChild(val);
      if (r.unit) box.appendChild(el("div", "ro-unit", r.unit));
      wrap.appendChild(box);
      roNodes[r.key] = { val, tag };
    }
  })();

  function sourceForKind(kind) {
    if (kind === "gps") return parseSource(channelSourceByRole("gps"));
    if (kind === "heading") return parseSource(channelSourceByRole("heading"));
    if (kind === "sim") return { tag: "SIM", cls: "src-sim" };
    if (kind === "time") {
      const ts = (lastHealth && lastHealth.time_source) ? String(lastHealth.time_source).toUpperCase() : "—";
      return { tag: ts, cls: "src-time" };
    }
    return { tag: "OFF", cls: "src-off" };
  }

  function repaintConning() {
    statePending = false;
    const s = lastState;
    if (!s) return;

    // compass
    const hdg = Number(s.heading_true_deg);
    if (Number.isFinite(hdg)) {
      $("compass-card").setAttribute("transform", "rotate(" + (-hdg) + " 100 100)");
      $("cmp-hdg").textContent = num(hdg, 0);
    }
    const cog = Number(s.cog_deg);
    if (Number.isFinite(cog) && Number.isFinite(hdg)) {
      $("cog-marker").setAttribute("transform", "rotate(" + (cog - hdg) + " 100 100)");
      $("cmp-cog").textContent = num(cog, 0);
    }

    // rate of turn (full scale +/-30 dpm -> +/-90px)
    const rot = Number(s.rot_dpm) || 0;
    const scaled = Math.max(-30, Math.min(30, rot)) / 30 * 90;
    const bar = $("rot-bar");
    if (scaled >= 0) { bar.setAttribute("x", "100"); bar.setAttribute("width", scaled.toFixed(1)); }
    else { bar.setAttribute("x", (100 + scaled).toFixed(1)); bar.setAttribute("width", (-scaled).toFixed(1)); }
    $("rot-val").textContent = num(rot, 1);

    // inclinometer: rotate by -roll, translate horizon by pitch
    const roll = Number(s.roll_deg) || 0, pitch = Number(s.pitch_deg) || 0;
    $("incl-horizon").setAttribute("transform", "rotate(" + (-roll) + " 100 100) translate(0 " + (pitch * 2).toFixed(1) + ")");
    $("incl-roll").textContent = num(roll, 1);
    $("incl-pitch").textContent = num(pitch, 1);

    // wind rose
    const appAng = Number(s.app_wind_angle_deg);
    if (Number.isFinite(appAng)) $("wind-app").setAttribute("transform", "rotate(" + appAng + " 100 100)");
    const trueDir = Number(s.wind_dir_deg), hd = Number(s.heading_true_deg);
    if (Number.isFinite(trueDir) && Number.isFinite(hd)) $("wind-true").setAttribute("transform", "rotate(" + (trueDir - hd) + " 100 100)");
    $("wind-app-spd").textContent = num(s.app_wind_speed_kn, 1);
    $("wind-app-ang").textContent = num(s.app_wind_angle_deg, 0);
    $("wind-true-spd").textContent = num(s.wind_speed_kn, 1);
    $("wind-true-dir").textContent = num(s.wind_dir_deg, 0);

    // digital readouts + per-value source tags
    for (const r of READOUTS) {
      const n = roNodes[r.key];
      n.val.textContent = r.get(s);
      const src = sourceForKind(r.srcKind);
      n.tag.textContent = src.tag;
      n.tag.className = "src " + src.cls;
    }
    // gauge header source tags
    const g = parseSource(channelSourceByRole("gps"));
    const h = parseSource(channelSourceByRole("heading"));
    $("cmp-src").textContent = h.tag; $("cmp-src").className = "src " + h.cls;
    $("rot-src").textContent = h.tag; $("rot-src").className = "src " + h.cls;
  }

  function requestConningPaint() {
    if (statePending) return;
    statePending = true;
    requestAnimationFrame(repaintConning);
  }

  // stale detector
  setInterval(() => {
    const stale = lastStateTs && (Date.now() - lastStateTs > STALE_MS);
    $("view-conning").classList.toggle("stale", !!stale);
  }, 1000);

  function updateSourceStrip() {
    const chips = $("src-chips");
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
    $("strip-mode").textContent = (lastHealth && lastHealth.mode) || (cfg && cfg.mode) || "—";
    $("strip-time").textContent = (lastHealth && lastHealth.time_source) ? String(lastHealth.time_source).toUpperCase() : "—";
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
