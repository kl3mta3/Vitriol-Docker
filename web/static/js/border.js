/* Vitriol website — inscribed alchemical border.
 *
 * Renders a fixed-position SVG that wraps the entire viewport with the
 * same composition the desktop app's BorderFrame paints around its
 * central widget:
 *
 *   - Two thin concentric rectangles, ~14 px gap between them.
 *   - 7 alchemical planetary glyphs (Sun/Moon/Mercury/Venus/Mars/Jupiter/Saturn)
 *     marching evenly around the perimeter, in clockwise order.
 *   - Filled diamond glyphs at each of the four corners as visual anchors.
 *
 * All in a single hue (purple #a78bfa) at four opacities, matching the
 * QSS BorderFrame palette exactly so the website and app feel like one
 * product. The border re-renders on resize (debounced) so it stays
 * proportional at any viewport size.
 *
 * Also handles:
 *   - Reveal-on-scroll (.reveal -> .reveal.in via IntersectionObserver)
 *   - Mobile menu toggle (if/when added; currently nav links collapse via CSS)
 */

(function () {
  "use strict";

  // --- Constants — match BorderFrame.py 1:1 ---------------------------------

  const PLANETARY_GLYPHS = ["☉", "☽", "☿", "♀", "♂", "♃", "♄"];
  const CORNER_GLYPH = "◆";
  // Default fallback hue — exactly the value the original const had
  // before the theme system existed. Used when --purple-soft isn't
  // defined on :root (very-early boot, malformed CSS) so the border
  // never renders invisible.
  const FALLBACK_HUE = "#a78bfa";

  // Read the active theme's accent color from CSS custom properties.
  // Each theme defines --purple-soft to its own palette (crimson red,
  // verdant green, parchment violet, obsidian off-white, etc.), so
  // calling this at every render makes the alchemy border re-color
  // automatically when the operator switches themes.
  function currentHue() {
    try {
      const v = getComputedStyle(document.documentElement)
        .getPropertyValue('--purple-soft').trim();
      return v || FALLBACK_HUE;
    } catch (_) {
      return FALLBACK_HUE;
    }
  }

  // Layout (px). Tuned for a viewport-scale border.
  const OUTER_INSET = 12;     // outer rectangle inset from viewport edge
  const LINE_GAP = 18;        // gap between outer and inner rectangles
  const CORNER_CLEARANCE = 32;// distance from corner to first glyph
  const TARGET_SPACING = 56;  // desired pixel spacing between glyphs
  const GLYPH_FONT_PX = 14;
  const CORNER_FONT_PX = 16;

  // Opacity matches BorderFrame.py 30 / 50 / 55 / 70 percent.
  const OUTER_ALPHA = 0.30;
  const INNER_ALPHA = 0.50;
  const GLYPH_ALPHA = 0.55;
  const CORNER_ALPHA = 0.70;

  // --- DOM helpers ---------------------------------------------------------

  const SVGNS = "http://www.w3.org/2000/svg";

  function svgEl(name, attrs) {
    const el = document.createElementNS(SVGNS, name);
    if (attrs) {
      for (const k in attrs) el.setAttribute(k, attrs[k]);
    }
    return el;
  }

  // --- Border rendering ----------------------------------------------------

  function renderBorder() {
    const svg = document.getElementById("alchemy-border");
    if (!svg) return;

    const w = window.innerWidth;
    const h = window.innerHeight;

    // Skip ridiculously narrow viewports — the border would be cramped.
    if (w < 360 || h < 360) {
      svg.innerHTML = "";
      return;
    }

    // Reset SVG to current viewport dimensions.
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("width", w);
    svg.setAttribute("height", h);
    svg.innerHTML = "";

    // Read the theme accent once per render, then pass it down to
    // every stroke/fill so a single re-render re-skins the whole
    // border in the new color.
    const hue = currentHue();

    const outer = {
      x: OUTER_INSET, y: OUTER_INSET,
      w: w - 2 * OUTER_INSET,
      h: h - 2 * OUTER_INSET,
    };
    const inner = {
      x: outer.x + LINE_GAP, y: outer.y + LINE_GAP,
      w: outer.w - 2 * LINE_GAP,
      h: outer.h - 2 * LINE_GAP,
    };

    // --- Two concentric rectangles ---
    svg.appendChild(svgEl("rect", {
      x: outer.x, y: outer.y, width: outer.w, height: outer.h,
      fill: "none", stroke: hue,
      "stroke-opacity": OUTER_ALPHA, "stroke-width": 1,
    }));
    svg.appendChild(svgEl("rect", {
      x: inner.x, y: inner.y, width: inner.w, height: inner.h,
      fill: "none", stroke: hue,
      "stroke-opacity": INNER_ALPHA, "stroke-width": 1,
    }));

    // --- Glyph row, all four edges, clockwise ---
    const topYMid = (outer.y + inner.y) / 2;
    const botYMid = ((outer.y + outer.h) + (inner.y + inner.h)) / 2;
    const leftXMid = (outer.x + inner.x) / 2;
    const rightXMid = ((outer.x + outer.w) + (inner.x + inner.w)) / 2;

    let glyphIndex = 0;

    // Top: left -> right
    let usable = outer.w - 2 * CORNER_CLEARANCE;
    let n = Math.max(1, Math.round(usable / TARGET_SPACING));
    let spacing = usable / n;
    for (let i = 0; i < n; i++) {
      const x = outer.x + CORNER_CLEARANCE + spacing * (i + 0.5);
      drawGlyph(svg, PLANETARY_GLYPHS[glyphIndex % 7], x, topYMid,
                GLYPH_FONT_PX, GLYPH_ALPHA, hue);
      glyphIndex++;
    }
    // Right: top -> bottom
    usable = outer.h - 2 * CORNER_CLEARANCE;
    n = Math.max(1, Math.round(usable / TARGET_SPACING));
    spacing = usable / n;
    for (let i = 0; i < n; i++) {
      const y = outer.y + CORNER_CLEARANCE + spacing * (i + 0.5);
      drawGlyph(svg, PLANETARY_GLYPHS[glyphIndex % 7], rightXMid, y,
                GLYPH_FONT_PX, GLYPH_ALPHA, hue);
      glyphIndex++;
    }
    // Bottom: right -> left (so the sequence reads clockwise around the page)
    usable = outer.w - 2 * CORNER_CLEARANCE;
    n = Math.max(1, Math.round(usable / TARGET_SPACING));
    spacing = usable / n;
    for (let i = 0; i < n; i++) {
      const x = outer.x + outer.w - CORNER_CLEARANCE - spacing * (i + 0.5);
      drawGlyph(svg, PLANETARY_GLYPHS[glyphIndex % 7], x, botYMid,
                GLYPH_FONT_PX, GLYPH_ALPHA, hue);
      glyphIndex++;
    }
    // Left: bottom -> top
    usable = outer.h - 2 * CORNER_CLEARANCE;
    n = Math.max(1, Math.round(usable / TARGET_SPACING));
    spacing = usable / n;
    for (let i = 0; i < n; i++) {
      const y = outer.y + outer.h - CORNER_CLEARANCE - spacing * (i + 0.5);
      drawGlyph(svg, PLANETARY_GLYPHS[glyphIndex % 7], leftXMid, y,
                GLYPH_FONT_PX, GLYPH_ALPHA, hue);
      glyphIndex++;
    }

    // --- Corner diamonds ---
    const gap = LINE_GAP / 2;
    const corners = [
      [outer.x + gap, outer.y + gap],
      [outer.x + outer.w - gap, outer.y + gap],
      [outer.x + outer.w - gap, outer.y + outer.h - gap],
      [outer.x + gap, outer.y + outer.h - gap],
    ];
    for (const [cx, cy] of corners) {
      drawGlyph(svg, CORNER_GLYPH, cx, cy, CORNER_FONT_PX, CORNER_ALPHA, hue);
    }
  }

  function drawGlyph(svg, ch, cx, cy, fontPx, alpha, hue) {
    const t = svgEl("text", {
      x: cx, y: cy,
      "text-anchor": "middle",
      "dominant-baseline": "central",
      "font-size": fontPx,
      "font-family": "system-ui, -apple-system, 'Segoe UI Symbol', sans-serif",
      fill: hue,
      "fill-opacity": alpha,
    });
    t.textContent = ch;
    svg.appendChild(t);
  }

  // --- Debounced resize ----------------------------------------------------
  // requestAnimationFrame coalesces multiple resize events into one paint —
  // smoother than a setTimeout debounce when the user drags the window.

  let resizeRaf = 0;
  function onResize() {
    if (resizeRaf) cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      renderBorder();
    });
  }

  // --- Reveal-on-scroll ----------------------------------------------------
  // Adds .in to .reveal elements when they enter the viewport. CSS handles
  // the actual transition. IntersectionObserver is the modern, low-overhead
  // way to do this — no scroll listener needed.

  function setupReveal() {
    const els = document.querySelectorAll(".reveal");
    if (!els.length || !("IntersectionObserver" in window)) {
      // Fallback: just show everything immediately.
      els.forEach((el) => el.classList.add("in"));
      return;
    }
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      }
    }, { threshold: 0.1, rootMargin: "0px 0px -40px 0px" });
    els.forEach((el) => io.observe(el));
  }

  // --- Init ---------------------------------------------------------------

  // Re-render whenever the theme changes (operator clicks a swatch on
  // the profile page, which toggles `data-theme` on <html>). The
  // MutationObserver listens directly so we don't need profile.js to
  // know about us — anyone who flips the attribute triggers a repaint.
  function watchThemeChanges() {
    try {
      const obs = new MutationObserver(() => renderBorder());
      obs.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme", "class"],
      });
    } catch (_) {
      // Older browsers without MutationObserver — they'll just keep
      // whatever color the initial render produced. Not a regression.
    }
  }

  function init() {
    renderBorder();
    window.addEventListener("resize", onResize, { passive: true });
    watchThemeChanges();
    setupReveal();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
