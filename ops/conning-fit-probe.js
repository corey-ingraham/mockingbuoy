/*
 * conning-fit-probe.js — measure whether the conning display actually fits a given viewport.
 *
 * WHY THIS EXISTS
 * The conning layout is a one-screen lock. When it does not fit, it fails SILENTLY: panels clip
 * under `.ins-panel { overflow: auto }` and columns overflow with their last panel simply hanging
 * outside the box. Nothing in the UI says so. Every previous generation of these numbers was taken
 * by hand, recorded in a comment or an issue, and rotted — see the register entries for the
 * conning-fit work. This script exists so the numbers are re-derivable in one paste.
 *
 * HOW TO RUN (no hardware, no appliance)
 *   MOCKINGBUOY_CONFIG=config.json python -m uvicorn web.app:app --host 127.0.0.1 --port 8010 --workers 1
 * Open http://127.0.0.1:8010/ in Chrome, paste this file into the DevTools console, then:
 *   await mbFit.sweep()                       // the standard viewport table
 *   await mbFit.at(1920, 1080)                // one viewport, full detail
 *   await mbFit.scaleSweep(1920, 870, [0.74, 0.70, 0.66])
 *   await mbFit.at(1920, 1080, mbFit.CSS_CANDIDATE)   // with a trial CSS override
 *
 * HOW IT WORKS
 * Measurement happens inside a same-origin <iframe> sized to exact CSS pixels. The iframe's element
 * size IS its layout viewport, so `max-height` media queries and `vh` units resolve against the
 * requested geometry — a true emulation without CDP. The wrapper is visually scaled down with a
 * transform so a 2560px-wide probe still fits on a laptop screen; a transform does not change the
 * layout viewport, so it does not perturb the measurement.
 *
 * TWO TRAPS THIS SCRIPT ENCODES — both cost real debugging time:
 *
 * 1. PER-PANEL OVERFLOW IS NOT ENOUGH. `scrollHeight - clientHeight` per `.ins-panel` reports
 *    "fits" while a column overflows and its last panel (Alerts / AIS) hangs entirely outside the
 *    column box. Measured at 1920x830: zero panel overflow, and Alerts cut off by 169px. A fit
 *    check that only walks panels prints a green tick over a visibly broken display. Always
 *    measure the COLUMNS and the tail-panel-vs-column-bottom delta too.
 *
 * 2. NEVER `requestAnimationFrame` HERE. rAF does not fire in a backgrounded tab, so an
 *    rAF-sequenced probe hangs forever instead of returning. Reading `scrollHeight` forces layout
 *    synchronously, so a plain property read after a style write is sufficient. (The same hazard
 *    applies to any in-app auto-fit: gate it on document.visibilityState or use a timeout fallback,
 *    never a bare rAF chain.)
 *
 * Tolerance is 2px, matching the sub-pixel residual the layout CSS already documents.
 */

(function () {
  "use strict";

  const TOL = 2;
  const SETTLE_MS = 600; // after load: let the SSE state frame paint real values into the gauges
  const LOAD_MS = 4000;

  /* Standard sweep. Each row's expected --ui-scale is asserted, because the original mis-measurement
     was taken in a windowed browser (innerHeight ~940) while labelled "1080" — i.e. it measured the
     0.74 tier and concluded the 1.0 tier was clean. A viewport whose computed scale does not match
     expectation is reported as a GATE FAILURE, not silently averaged in. */
  const VIEWPORTS = [
    { w: 1920, h: 1080, expect: "1" },    // fullscreen 1080p — the real kiosk tier
    { w: 1920, h: 1000, expect: "0.82" },
    { w: 1920, h: 940, expect: "0.74" },  // 1080p in a WINDOWED browser
    { w: 2560, h: 1440, expect: "1" },
    { w: 1280, h: 1024, expect: "1" },
    { w: 1920, h: 870, expect: "0.74" },  // the tight band above the scroll threshold
    { w: 1100, h: 900, expect: "0.74" },  // narrow: scroll tier, one column
  ];

  async function withProbe(w, h, css, fn) {
    document.getElementById("mb-probe-wrap")?.remove();
    const wrap = document.createElement("div");
    wrap.id = "mb-probe-wrap";
    wrap.style.cssText =
      "position:fixed;left:0;top:0;z-index:99999;transform-origin:0 0;pointer-events:none;" +
      "opacity:0.01;transform:scale(" + 200 / w + ");";
    const f = document.createElement("iframe");
    f.style.cssText = "border:0;display:block;width:" + w + "px;height:" + h + "px;";
    f.src = "/";
    wrap.appendChild(f);
    document.body.appendChild(wrap);
    try {
      await new Promise((res) => {
        f.onload = res;
        setTimeout(res, LOAD_MS);
      });
      await new Promise((res) => setTimeout(res, SETTLE_MS));
      const d = f.contentDocument;
      if (!d) throw new Error("no contentDocument — is the probe same-origin?");
      if (css) {
        const st = d.createElement("style");
        st.textContent = css;
        d.head.appendChild(st);
      }
      return await fn(d, f.contentWindow);
    } finally {
      wrap.remove();
    }
  }

  /* The complete metric. `worst` is what an auto-fit loop should minimise: the max over
     per-panel internal overflow, per-column overflow, and how far each column's last panel
     hangs below its column. Any one of the three alone can read clean while the display is broken. */
  function measure(d, win) {
    const view = d.getElementById("view-conning");
    const colL = d.querySelector(".conn-col-left");
    const colR = d.querySelector(".conn-col-right");
    const ins = d.querySelector(".conn-ins");
    void d.body.offsetHeight; // force layout before any read

    const panels = [...d.querySelectorAll("#view-conning .ins-panel")].map((el) => ({
      cls: (el.className.match(/p-[a-z]+/) || ["?"])[0],
      oy: el.scrollHeight - el.clientHeight,
      ox: el.scrollWidth - el.clientWidth,
    }));
    const over = (el) => (el ? el.scrollHeight - el.clientHeight : 0);
    const tailCut = (col) => {
      if (!col || !col.lastElementChild) return 0;
      return Math.round(
        col.lastElementChild.getBoundingClientRect().bottom - col.getBoundingClientRect().bottom
      );
    };

    const cs = win.getComputedStyle(view);
    const scrolling = cs.overflowY === "auto"; // the scroll tiers unlock the one-screen layout
    const m = {
      vw: win.innerWidth,
      vh: win.innerHeight,
      scale: cs.getPropertyValue("--ui-scale").trim(),
      manualScale: view.style.getPropertyValue("--ui-scale").trim() || null,
      tier: scrolling
        ? win.matchMedia("(max-width: 1100px)").matches
          ? "scrolling (narrow)"
          : "scrolling (short)"
        : "one-screen",
      colLover: over(colL),
      colRover: over(colR),
      insOver: over(ins),
      tailLcut: tailCut(colL),
      tailRcut: tailCut(colR),
      clipY: panels.filter((p) => p.oy > TOL),
      clipX: panels.filter((p) => p.ox > TOL),
      nPanels: panels.length,
    };
    // In a scroll tier the view is MEANT to scroll, so view overflow is not a defect there;
    // column overflow and clipped panels still are.
    m.worst = Math.max(
      0,
      m.colLover,
      m.colRover,
      m.tailLcut,
      m.tailRcut,
      ...m.clipY.map((p) => p.oy),
      ...m.clipX.map((p) => p.ox)
    );
    m.fits = m.worst <= TOL;
    return m;
  }

  const fmt = (m) =>
    m.vw +
    "x" + m.vh +
    " s" + m.scale +
    " " + m.tier +
    " | worst " + m.worst +
    " (colL " + m.colLover + " colR " + m.colRover +
    " tailL " + m.tailLcut + " tailR " + m.tailRcut + ")" +
    " | clipY " + (m.clipY.map((p) => p.cls + ":" + p.oy).join(" ") || "-") +
    " | clipX " + (m.clipX.map((p) => p.cls + ":" + p.ox).join(" ") || "-") +
    " | " + (m.fits ? "FITS" : "DOES NOT FIT");

  const mbFit = {
    TOL,
    VIEWPORTS,

    /* Slot for trialling a CSS change before editing app.css: pass it as the `css` argument to
       at()/sweep()/scaleSweep() and it is injected into the probe frame only.
       The scale-relative floors this was used to derive now live in app.css; do not re-state their
       values here, or this file becomes another stale copy of numbers that have moved on. What is
       worth recording is HOW they were derived: fit each floor as an affine function of --ui-scale
       through MEASURED-good points, across the whole usable density range, not just the top of it.
       A plain `Npx * scale` undershoots at low density (part of each panel is scale-invariant:
       unscaled label fonts, vh-capped dials) and the panel then clips WORSE as density drops, which
       silently defeats any step-down auto-fit. A fixed px floor overshoots the other way and pushes
       the column over on shorter screens. */
    CSS_CANDIDATE: "",

    async at(w, h, css) {
      const m = await withProbe(w, h, css, measure);
      console.log(fmt(m));
      return m;
    },

    async sweep(css, viewports) {
      const list = viewports || VIEWPORTS;
      const rows = [];
      for (const v of list) {
        const m = await withProbe(v.w, v.h, css, measure);
        const gate =
          v.expect && m.scale !== v.expect
            ? "  <== GATE FAILURE: expected --ui-scale " + v.expect + ", got " + m.scale
            : "";
        rows.push(fmt(m) + gate);
        console.log(rows[rows.length - 1]);
      }
      const bad = rows.filter((r) => r.includes("DOES NOT FIT") || r.includes("GATE FAILURE"));
      console.log(bad.length ? "\n" + bad.length + " viewport(s) failing" : "\nall viewports fit");
      return rows;
    },

    /* Find the largest scale that fits, the way an in-app auto-fit would. Bidirectional: a large
       monitor needs to grow, not only shrink. Returns null when NO scale in range fits — which is a
       real outcome, not an error: in the ~821-870px band the residual is scale-invariant content
       and plateaus around 17px. Auto must decline there rather than pinning the floor scale. */
    async scaleSweep(w, h, scales, css) {
      const list = scales || [1.2, 1.1, 1.0, 0.92, 0.84, 0.8, 0.76, 0.72, 0.68, 0.66];
      return withProbe(w, h, css, async (d, win) => {
        const view = d.getElementById("view-conning");
        const out = [];
        let best = null;
        for (const s of list) {
          view.style.setProperty("--ui-scale", String(s));
          const m = measure(d, win);
          out.push(s + "=" + m.worst);
          if (m.fits && best === null) best = s;
        }
        console.log(w + "x" + h + "  " + out.join("  ") + "  => largest fitting: " + best);
        return { largestFitting: best, trace: out };
      });
    },
  };

  window.mbFit = mbFit;
  console.log("mbFit ready — try: await mbFit.sweep()   |   await mbFit.sweep(mbFit.CSS_CANDIDATE)");
})();
