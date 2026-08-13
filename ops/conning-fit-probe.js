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
 * FOUR TRAPS THIS SCRIPT ENCODES — all cost real debugging time:
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
 *    never a bare rAF chain. Note `ResizeObserver` is NOT an escape: its callbacks are delivered in
 *    the same "update the rendering" step as rAF, so a hidden document defers both.)
 *
 * 3. OVERFLOW METRICS CANNOT SEE INTRA-PANEL OVERLAP. A child that spills its flex box paints over
 *    its next sibling without ever growing the panel's scrollHeight — the panel is at its floor, so
 *    there is nothing to scroll. That is how the ENV readings row shipped to a lab monitor painted
 *    across both wind dials while this script printed FITS. `overlaps` closes that gap, and it must
 *    test BOTH axes: a vertical-interval test alone fires on every side-by-side row in the display
 *    and produces a gate that fails identically before and after a fix, i.e. no gate at all.
 *
 * 4. "IT FITS" IS NOT "IT IS LEGIBLE". A gauge allowed to shrink so the layout fits is the same
 *    defect one level down — see the "40x20 postage stamp" episode in app.css. Any panel whose
 *    contents are allowed to compress needs a floor AND a metric watching that floor (`dialMin`),
 *    or the fix converts a visible failure into an invisible one.
 *
 * Tolerance is 2px, matching the sub-pixel residual the layout CSS already documents.
 */

(function () {
  "use strict";

  const TOL = 2;
  /* Overlap needs its OWN, looser threshold. TOL is a sub-pixel overflow residual; ink boxes are a
     different measurement. Stacked label/value pairs (`rot-label`/`rot-value`, `wind-stat-t`/
     `wind-stat-v`) genuinely intersect by 3-4px because a line box is taller than its glyphs, so
     at TOL those pairs report on every run, before and after any fix -- noise that reads as a
     finding. Set above that floor; the defect this exists to catch measured 10px. */
  const OVL_TOL = 6;
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

  /* Sibling overlap. MUST test BOTH axes: a vertical-interval test alone fires on every
     side-by-side row in this display (the two .env-dial columns, the four .env-cell readings,
     the .wind-stat pairs, the two .depth-vals items) and would report every viewport broken
     before AND after any fix -- a gate that can never pass is worse than no gate. Two boxes
     only genuinely collide when their x-projections and y-projections both intersect. */
  /* Compare INK boxes, not layout boxes. The defect this exists to catch is a descendant painting
     OUTSIDE its own box, across a box that is not its parent's sibling but its uncle's: the wind
     dial SVG spills out of `.env-dials` (overflow: visible, so that box does not grow) and lands on
     `.env-readings`. Comparing `.env-dials` to `.env-readings` as laid-out rects therefore reports
     nothing — measured: a real 8px collision reported as zero. The ink box (union of an element's
     own rect with every descendant's) is what the operator actually sees, and SVGs must be included
     rather than skipped, since the dial IS the thing that overflows. */
  /* An <svg> is a LEAF here: it clips to its own viewport, so its ink is exactly its border box.
     Descending into one instead compares circles against needles against tick groups — deliberately
     overlapping artwork — and buries the real finding under hundreds of false hits (measured: 285). */
  const isSvg = (el) => el.tagName.toLowerCase() === "svg";
  const label = (el) => el.getAttribute("class") || el.id || el.tagName.toLowerCase();

  function inkBox(el) {
    const r = el.getBoundingClientRect();
    let [t, b, l, x] = [r.top, r.bottom, r.left, r.right];
    if (!isSvg(el)) {
      for (const kid of el.querySelectorAll("*")) {
        if (kid.closest("svg") && !isSvg(kid)) continue; // inside an svg: covered by the svg's own box
        if (!kid.getClientRects().length) continue;
        const k = kid.getBoundingClientRect();
        if (!k.width && !k.height) continue;
        t = Math.min(t, k.top); b = Math.max(b, k.bottom);
        l = Math.min(l, k.left); x = Math.max(x, k.right);
      }
    }
    return { top: t, bottom: b, left: l, right: x };
  }

  function overlapPairs(d) {
    const out = [];
    for (const panel of d.querySelectorAll("#view-conning .ins-panel")) {
      const cls = (panel.className.match(/p-[a-z]+/) || ["?"])[0];
      const walk = (parent) => {
        if (isSvg(parent)) return;
        const kids = [...parent.children].filter((el) => el.getClientRects().length > 0);
        const boxes = kids.map(inkBox);
        for (let i = 0; i < kids.length; i++) {
          for (let j = i + 1; j < kids.length; j++) {
            const a = boxes[i], b = boxes[j];
            const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
            if (oy > OVL_TOL && ox > OVL_TOL) {
              out.push({ cls, a: label(kids[i]), b: label(kids[j]),
                         ox: Math.round(ox), oy: Math.round(oy), worst: Math.round(Math.min(ox, oy)) });
            }
          }
          walk(kids[i]);
        }
      };
      walk(panel);
    }
    return out;
  }

  /* Fill ratio of a viewBox'd SVG inside its wrapper: how much of the box the DRAWING covers
     once preserveAspectRatio="meet" has letterboxed it. Computed from the viewBox attribute and
     the CSS box, NOT getBBox -- getBBox measures whatever happened to be drawn, so an empty or
     placeholder chart would read as a layout defect.
     This is also why the probe needs no depth-history seeding: a frame measured at SETTLE_MS has
     had <2 samples at 1 Hz and shows only "acquiring depth…", but the viewBox is set before that
     early return, so the ratio still reports layout rather than SSE connect timing. */
  function fillRatio(svg, wrap) {
    if (!svg || !wrap) return null;
    const vb = (svg.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
    const r = wrap.getBoundingClientRect();
    if (vb.length !== 4 || !vb[2] || !vb[3] || !r.width || !r.height) return null;
    const k = Math.min(r.width / vb[2], r.height / vb[3]); // meet: the smaller ratio wins
    return { x: +(vb[2] * k / r.width).toFixed(3), y: +(vb[3] * k / r.height).toFixed(3),
             w: Math.round(r.width), h: Math.round(r.height) };
  }

  /* Smallest rendered wind-dial diameter. Fix for the ENV overlap makes the dials height-driven,
     which trades a VISIBLE overlap for an INVISIBLE one: without a floor they shrink toward
     nothing and every other metric still reads clean. This is that floor's instrument -- cf. the
     "40x20 postage stamp" episode recorded in app.css.
     Measure the DRAWN diameter, not the CSS box. The dial now fills its wrapper edge-to-edge and
     `preserveAspectRatio="meet"` letterboxes the face inside that box, so the border box is the
     wrapper's size no matter how small the compass actually renders -- reading it would report a
     constant and guard nothing. */
  function dialMin(d) {
    const dials = [...d.querySelectorAll(".env-dial svg")].map((s) => {
      const r = s.getBoundingClientRect();
      const vb = (s.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
      const k = vb.length === 4 && vb[2] && vb[3]
        ? Math.min(r.width / vb[2], r.height / vb[3]) : 1;
      return { id: s.id, h: Math.round(vb[3] * k), w: Math.round(vb[2] * k),
               boxH: Math.round(r.height) };
    });
    if (!dials.length) return null;
    return dials.reduce((a, b) => (b.h < a.h ? b : a));
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
      // Intra-panel overlap: the defect class the original metric was blind to. The ENV readings
      // painting over the wind dials reached a lab monitor while every metric above read "FITS".
      overlaps: overlapPairs(d),
      dialMin: dialMin(d),
      depthFill: fillRatio(d.getElementById("depth-graph"), d.querySelector(".depth-fill")),
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
      ...m.clipX.map((p) => p.ox),
      ...m.overlaps.map((o) => o.worst)
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
    " | overlap " + (m.overlaps.map((o) => o.cls + " " + o.a + "/" + o.b + ":" + o.worst).join("  ") || "-") +
    " | dial " + (m.dialMin ? m.dialMin.h + "px" : "-") +
    " | depthFill " + (m.depthFill ? m.depthFill.x + "/" + m.depthFill.y : "-") +
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

    /* Escape hatch for deriving a floor: run an arbitrary callback inside ONE probe frame, with
       `measure` handed to it. Each at()/sweep() call builds and loads its own iframe (~4.6s with
       LOAD_MS+SETTLE_MS), so a binary search driven from outside is ~50 loads and hangs the
       renderer -- which is how the .p-env re-derivation first failed. Mutate styles in `d` and
       re-measure in-place instead: layout is synchronous, so no reload is needed between probes. */
    async inFrame(w, h, css, fn) {
      return withProbe(w, h, css, (d, win) => fn(d, win, () => measure(d, win)));
    },

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
