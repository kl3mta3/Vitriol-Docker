/* Vitriol website — inscribed alchemical border.
 *
 * Renders a fixed-position SVG that wraps the entire viewport.
 * The active style is read from `data-border-style` on <html>:
 *
 *   vitriol  — planetary glyphs (☉☽☿♀♂♃♄) + ◆ corners  [default, unchanged]
 *   runes    — Elder Futhark runes (ᚠᚢᚦᚨᚱᚲᚷ) + ᛉ corners
 *   arcane   — geometric sigils (⊕⊗✦⊙⋆✧⊛) + ✦ corners
 *   circuit  — SVG tick marks + filled dots at glyph positions, ■ corners
 *   minimal  — two concentric rectangles only, no glyphs
 *
 * All styles share the same two-rectangle frame geometry (OUTER_INSET,
 * LINE_GAP) and opacity constants so they feel like the same product.
 *
 * Also handles:
 *   - Reveal-on-scroll (.reveal -> .reveal.in via IntersectionObserver)
 */

(function () {
  "use strict";

  // --- Glyph style table ----------------------------------------------------

  const GLYPH_STYLES = {
    vitriol: { glyphs: ["☉", "☽", "☿", "♀", "♂", "♃", "♄"], corner: "◆" },
    runes:   { glyphs: ["ᚠ", "ᚢ", "ᚦ", "ᚨ", "ᚱ", "ᚲ", "ᚷ"], corner: "ᛉ" },
    arcane:  { glyphs: ["⊕", "⊗", "✦", "⊙", "⋆", "✧", "⊛"], corner: "✦" },
    // circuit, minimal, vine, helix handled specially — no glyph array needed
  };

  // Default fallback hue — used before CSS custom properties are readable.
  const FALLBACK_HUE = "#a78bfa";

  function currentHue() {
    try {
      const v = getComputedStyle(document.documentElement)
        .getPropertyValue("--purple-soft").trim();
      return v || FALLBACK_HUE;
    } catch (_) {
      return FALLBACK_HUE;
    }
  }

  // --- Layout constants (match BorderFrame.py 1:1) --------------------------

  const OUTER_INSET      = 12;   // outer rectangle inset from viewport edge
  const LINE_GAP         = 18;   // gap between outer and inner rectangles
  const CORNER_CLEARANCE = 32;   // distance from corner to first glyph
  const TARGET_SPACING   = 56;   // desired pixel spacing between glyphs
  const GLYPH_FONT_PX    = 14;
  const CORNER_FONT_PX   = 16;

  const OUTER_ALPHA  = 0.30;
  const INNER_ALPHA  = 0.50;
  const GLYPH_ALPHA  = 0.55;
  const CORNER_ALPHA = 0.70;

  // --- DOM helpers ----------------------------------------------------------

  const SVGNS = "http://www.w3.org/2000/svg";

  function svgEl(name, attrs) {
    const el = document.createElementNS(SVGNS, name);
    if (attrs) for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
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

  // --- Shared frame geometry ------------------------------------------------

  function buildFrame(w, h) {
    const outer = { x: OUTER_INSET, y: OUTER_INSET,
                    w: w - 2 * OUTER_INSET, h: h - 2 * OUTER_INSET };
    const inner = { x: outer.x + LINE_GAP, y: outer.y + LINE_GAP,
                    w: outer.w - 2 * LINE_GAP, h: outer.h - 2 * LINE_GAP };
    return { outer, inner };
  }

  function drawRects(svg, outer, inner, hue) {
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
  }

  function cornerPositions(outer) {
    const gap = LINE_GAP / 2;
    return [
      [outer.x + gap,           outer.y + gap],
      [outer.x + outer.w - gap, outer.y + gap],
      [outer.x + outer.w - gap, outer.y + outer.h - gap],
      [outer.x + gap,           outer.y + outer.h - gap],
    ];
  }

  // Walk all four edges clockwise, calling cb(cx, cy, index) for each slot.
  function walkEdgeSlots(outer, cb) {
    const topY    = (outer.y + (outer.y + LINE_GAP)) / 2;
    const botY    = ((outer.y + outer.h) + (outer.y + outer.h - LINE_GAP)) / 2;
    const leftX   = (outer.x + (outer.x + LINE_GAP)) / 2;
    const rightX  = ((outer.x + outer.w) + (outer.x + outer.w - LINE_GAP)) / 2;
    let idx = 0;

    function edge(usableLen, getPoint) {
      const n = Math.max(1, Math.round(usableLen / TARGET_SPACING));
      const spacing = usableLen / n;
      for (let i = 0; i < n; i++) {
        const [cx, cy] = getPoint(i, spacing);
        cb(cx, cy, idx++);
      }
    }

    // Top: left → right
    edge(outer.w - 2 * CORNER_CLEARANCE, (i, sp) =>
      [outer.x + CORNER_CLEARANCE + sp * (i + 0.5), topY]);
    // Right: top → bottom
    edge(outer.h - 2 * CORNER_CLEARANCE, (i, sp) =>
      [rightX, outer.y + CORNER_CLEARANCE + sp * (i + 0.5)]);
    // Bottom: right → left (clockwise)
    edge(outer.w - 2 * CORNER_CLEARANCE, (i, sp) =>
      [outer.x + outer.w - CORNER_CLEARANCE - sp * (i + 0.5), botY]);
    // Left: bottom → top
    edge(outer.h - 2 * CORNER_CLEARANCE, (i, sp) =>
      [leftX, outer.y + outer.h - CORNER_CLEARANCE - sp * (i + 0.5)]);
  }

  // --- Style renderers ------------------------------------------------------

  // Shared by vitriol / runes / arcane — just different glyph arrays.
  function renderGlyphStyle(svg, outer, inner, hue, styleDef) {
    drawRects(svg, outer, inner, hue);

    walkEdgeSlots(outer, (cx, cy, idx) => {
      const ch = styleDef.glyphs[idx % styleDef.glyphs.length];
      drawGlyph(svg, ch, cx, cy, GLYPH_FONT_PX, GLYPH_ALPHA, hue);
    });

    for (const [cx, cy] of cornerPositions(outer)) {
      drawGlyph(svg, styleDef.corner, cx, cy, CORNER_FONT_PX, CORNER_ALPHA, hue);
    }
  }

  function renderCircuit(svg, outer, inner, hue) {
    drawRects(svg, outer, inner, hue);

    // Each glyph slot → a short tick mark (line) perpendicular to the edge,
    // plus a small filled circle at its midpoint.
    const TICK_HALF = 5;
    const DOT_R     = 2;

    walkEdgeSlots(outer, (cx, cy) => {
      // Determine orientation: horizontal slot (top/bottom) or vertical (left/right)
      const midX = outer.x + outer.w / 2;
      const midY = outer.y + outer.h / 2;
      const isHorizontal = Math.abs(cy - midY) > Math.abs(cx - midX);

      if (isHorizontal) {
        // top or bottom edge → vertical tick
        svg.appendChild(svgEl("line", {
          x1: cx, y1: cy - TICK_HALF, x2: cx, y2: cy + TICK_HALF,
          stroke: hue, "stroke-opacity": GLYPH_ALPHA, "stroke-width": 1,
        }));
      } else {
        // left or right edge → horizontal tick
        svg.appendChild(svgEl("line", {
          x1: cx - TICK_HALF, y1: cy, x2: cx + TICK_HALF, y2: cy,
          stroke: hue, "stroke-opacity": GLYPH_ALPHA, "stroke-width": 1,
        }));
      }
      svg.appendChild(svgEl("circle", {
        cx, cy, r: DOT_R,
        fill: hue, "fill-opacity": GLYPH_ALPHA,
      }));
    });

    // Small filled squares at corners
    const SQ = 5;
    for (const [cx, cy] of cornerPositions(outer)) {
      svg.appendChild(svgEl("rect", {
        x: cx - SQ / 2, y: cy - SQ / 2, width: SQ, height: SQ,
        fill: hue, "fill-opacity": CORNER_ALPHA,
      }));
    }
  }

  function renderMinimal(svg, outer, inner, hue) {
    drawRects(svg, outer, inner, hue);
    // No glyphs — just the two rectangles.
  }

  // --- Vine ---------------------------------------------------------------
  // Organic botanical style: a wavy bezier vine stem runs along each edge's
  // mid-stripe with small leaf ellipses at every peak. Corners get a trefoil
  // (three small circles arranged in a flower).

  function renderVine(svg, outer, inner, hue) {
    drawRects(svg, outer, inner, hue);

    const WAVE_AMP = 4;   // ±px perpendicular to the edge
    const LEAF_RX  = 6;   // semi-major axis of each leaf ellipse
    const LEAF_RY  = 2;   // semi-minor axis

    const topY  = (outer.y + (outer.y + LINE_GAP)) / 2;
    const botY  = ((outer.y + outer.h) + (outer.y + outer.h - LINE_GAP)) / 2;
    const leftX = (outer.x + (outer.x + LINE_GAP)) / 2;
    const rightX= ((outer.x + outer.w) + (outer.x + outer.w - LINE_GAP)) / 2;
    const cl    = CORNER_CLEARANCE;

    // Horizontal vine (top / bottom edge)
    function vineH(x0, x1, midY) {
      const len   = x1 - x0;
      const nHalf = Math.max(2, Math.round(len / (TARGET_SPACING * 0.55)));
      const seg   = len / nHalf;
      let d = `M ${x0},${midY}`;
      for (let i = 0; i < nHalf; i++) {
        const mx  = x0 + (i + 0.5) * seg;
        const ex  = x0 + (i + 1) * seg;
        const dir = i % 2 === 0 ? -1 : 1;
        d += ` Q ${mx},${midY + dir * WAVE_AMP} ${ex},${midY}`;
      }
      svg.appendChild(svgEl("path", {
        d, fill: "none", stroke: hue,
        "stroke-opacity": INNER_ALPHA, "stroke-width": 1.2,
      }));
      // Leaf at each wave peak
      for (let i = 0; i < nHalf; i++) {
        const px  = x0 + (i + 0.5) * seg;
        const dir = i % 2 === 0 ? -1 : 1;
        const py  = midY + dir * WAVE_AMP;
        const rot = dir * 25 + (i % 3 - 1) * 12;
        svg.appendChild(svgEl("ellipse", {
          cx: px, cy: py, rx: LEAF_RX, ry: LEAF_RY,
          transform: `rotate(${rot} ${px} ${py})`,
          fill: "none", stroke: hue,
          "stroke-opacity": GLYPH_ALPHA, "stroke-width": 0.9,
        }));
      }
    }

    // Vertical vine (left / right edge)
    function vineV(y0, y1, midX) {
      const len   = y1 - y0;
      const nHalf = Math.max(2, Math.round(len / (TARGET_SPACING * 0.55)));
      const seg   = len / nHalf;
      let d = `M ${midX},${y0}`;
      for (let i = 0; i < nHalf; i++) {
        const my  = y0 + (i + 0.5) * seg;
        const ey  = y0 + (i + 1) * seg;
        const dir = i % 2 === 0 ? -1 : 1;
        d += ` Q ${midX + dir * WAVE_AMP},${my} ${midX},${ey}`;
      }
      svg.appendChild(svgEl("path", {
        d, fill: "none", stroke: hue,
        "stroke-opacity": INNER_ALPHA, "stroke-width": 1.2,
      }));
      for (let i = 0; i < nHalf; i++) {
        const py  = y0 + (i + 0.5) * seg;
        const dir = i % 2 === 0 ? -1 : 1;
        const px  = midX + dir * WAVE_AMP;
        // +90° so the leaf points away from the edge (not along it)
        const rot = dir * 25 + (i % 3 - 1) * 12 + 90;
        svg.appendChild(svgEl("ellipse", {
          cx: px, cy: py, rx: LEAF_RX, ry: LEAF_RY,
          transform: `rotate(${rot} ${px} ${py})`,
          fill: "none", stroke: hue,
          "stroke-opacity": GLYPH_ALPHA, "stroke-width": 0.9,
        }));
      }
    }

    vineH(outer.x + cl, outer.x + outer.w - cl, topY);
    vineH(outer.x + cl, outer.x + outer.w - cl, botY);
    vineV(outer.y + cl, outer.y + outer.h - cl, leftX);
    vineV(outer.y + cl, outer.y + outer.h - cl, rightX);

    // Trefoil corner: three small circles arranged around the corner point
    const CR = 3;
    for (const [cx, cy] of cornerPositions(outer)) {
      const offsets = [[0, -CR * 2.2], [CR * 1.9, CR * 1.1], [-CR * 1.9, CR * 1.1]];
      for (const [dx, dy] of offsets) {
        svg.appendChild(svgEl("circle", {
          cx: cx + dx, cy: cy + dy, r: CR,
          fill: "none", stroke: hue, "stroke-opacity": CORNER_ALPHA, "stroke-width": 1,
        }));
      }
    }
  }

  // --- Helix (DNA double-helix) -------------------------------------------
  // Two phase-inverted sine waves run along each edge's mid-stripe.
  // Short rung lines connect the strands at their peak positions (base pairs).
  // Corners get two concentric circles.

  function renderHelix(svg, outer, inner, hue) {
    drawRects(svg, outer, inner, hue);

    const WAVE_AMP  = 5;   // ±px perpendicular
    const HALF_WAVE = 22;  // target px per half-wave (~44px full period)
    const RUNG_STEP = 2;   // draw a rung every N half-waves

    const topY  = (outer.y + (outer.y + LINE_GAP)) / 2;
    const botY  = ((outer.y + outer.h) + (outer.y + outer.h - LINE_GAP)) / 2;
    const leftX = (outer.x + (outer.x + LINE_GAP)) / 2;
    const rightX= ((outer.x + outer.w) + (outer.x + outer.w - LINE_GAP)) / 2;
    const cl    = CORNER_CLEARANCE;

    // Horizontal helix (top / bottom edge)
    function helixH(x0, x1, midY) {
      const len   = x1 - x0;
      const nHalf = Math.max(4, Math.round(len / HALF_WAVE));
      const seg   = len / nHalf;
      let d1 = `M ${x0},${midY}`;
      let d2 = `M ${x0},${midY}`;
      for (let i = 0; i < nHalf; i++) {
        const mx  = x0 + (i + 0.5) * seg;
        const ex  = x0 + (i + 1) * seg;
        const dir = i % 2 === 0 ? -1 : 1;
        d1 += ` Q ${mx},${midY + dir * WAVE_AMP} ${ex},${midY}`;
        d2 += ` Q ${mx},${midY - dir * WAVE_AMP} ${ex},${midY}`;
      }
      svg.appendChild(svgEl("path", { d: d1, fill: "none", stroke: hue, "stroke-opacity": INNER_ALPHA, "stroke-width": 1 }));
      svg.appendChild(svgEl("path", { d: d2, fill: "none", stroke: hue, "stroke-opacity": GLYPH_ALPHA, "stroke-width": 1 }));
      // Rungs at peak positions (every RUNG_STEP half-waves)
      for (let i = 0; i < nHalf; i += RUNG_STEP) {
        const rx  = x0 + (i + 0.5) * seg;
        const dir = i % 2 === 0 ? -1 : 1;
        svg.appendChild(svgEl("line", {
          x1: rx, y1: midY + dir * WAVE_AMP,
          x2: rx, y2: midY - dir * WAVE_AMP,
          stroke: hue, "stroke-opacity": GLYPH_ALPHA * 0.55, "stroke-width": 0.8,
        }));
      }
    }

    // Vertical helix (left / right edge)
    function helixV(y0, y1, midX) {
      const len   = y1 - y0;
      const nHalf = Math.max(4, Math.round(len / HALF_WAVE));
      const seg   = len / nHalf;
      let d1 = `M ${midX},${y0}`;
      let d2 = `M ${midX},${y0}`;
      for (let i = 0; i < nHalf; i++) {
        const my  = y0 + (i + 0.5) * seg;
        const ey  = y0 + (i + 1) * seg;
        const dir = i % 2 === 0 ? -1 : 1;
        d1 += ` Q ${midX + dir * WAVE_AMP},${my} ${midX},${ey}`;
        d2 += ` Q ${midX - dir * WAVE_AMP},${my} ${midX},${ey}`;
      }
      svg.appendChild(svgEl("path", { d: d1, fill: "none", stroke: hue, "stroke-opacity": INNER_ALPHA, "stroke-width": 1 }));
      svg.appendChild(svgEl("path", { d: d2, fill: "none", stroke: hue, "stroke-opacity": GLYPH_ALPHA, "stroke-width": 1 }));
      for (let i = 0; i < nHalf; i += RUNG_STEP) {
        const ry  = y0 + (i + 0.5) * seg;
        const dir = i % 2 === 0 ? -1 : 1;
        svg.appendChild(svgEl("line", {
          x1: midX + dir * WAVE_AMP, y1: ry,
          x2: midX - dir * WAVE_AMP, y2: ry,
          stroke: hue, "stroke-opacity": GLYPH_ALPHA * 0.55, "stroke-width": 0.8,
        }));
      }
    }

    helixH(outer.x + cl, outer.x + outer.w - cl, topY);
    helixH(outer.x + cl, outer.x + outer.w - cl, botY);
    helixV(outer.y + cl, outer.y + outer.h - cl, leftX);
    helixV(outer.y + cl, outer.y + outer.h - cl, rightX);

    // Corner: two concentric circles (outer ring + filled inner dot)
    for (const [cx, cy] of cornerPositions(outer)) {
      svg.appendChild(svgEl("circle", {
        cx, cy, r: 5,
        fill: "none", stroke: hue, "stroke-opacity": CORNER_ALPHA, "stroke-width": 1,
      }));
      svg.appendChild(svgEl("circle", {
        cx, cy, r: 2,
        fill: hue, "fill-opacity": CORNER_ALPHA,
      }));
    }
  }

  // --- Main render ----------------------------------------------------------

  function renderBorder() {
    const svg = document.getElementById("alchemy-border");
    if (!svg) return;

    const showBorder = document.documentElement.getAttribute("data-show-border");
    if (showBorder === "off") {
      svg.innerHTML = "";
      return;
    }

    // clientWidth/Height excludes the scrollbar on Windows/Linux.
    // innerWidth includes it, which would paint the right/bottom frame
    // lines behind the scrollbar track and clip them visually.
    const w = document.documentElement.clientWidth;
    const h = document.documentElement.clientHeight;

    if (w < 360 || h < 360) {
      svg.innerHTML = "";
      return;
    }

    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("width", w);
    svg.setAttribute("height", h);
    svg.innerHTML = "";

    const hue = currentHue();
    const { outer, inner } = buildFrame(w, h);
    const style = document.documentElement.getAttribute("data-border-style") || "vitriol";

    if (style === "circuit") {
      renderCircuit(svg, outer, inner, hue);
    } else if (style === "minimal") {
      renderMinimal(svg, outer, inner, hue);
    } else if (style === "vine") {
      renderVine(svg, outer, inner, hue);
    } else if (style === "helix") {
      renderHelix(svg, outer, inner, hue);
    } else {
      // vitriol, runes, arcane — or any unknown value falls back to vitriol
      renderGlyphStyle(svg, outer, inner, hue, GLYPH_STYLES[style] || GLYPH_STYLES.vitriol);
    }
  }

  // --- Debounced resize -----------------------------------------------------

  let resizeRaf = 0;
  function onResize() {
    if (resizeRaf) cancelAnimationFrame(resizeRaf);
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      renderBorder();
    });
  }

  // --- Reveal-on-scroll -----------------------------------------------------

  function setupReveal() {
    const els = document.querySelectorAll(".reveal");
    if (!els.length || !("IntersectionObserver" in window)) {
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

  // --- Init -----------------------------------------------------------------

  function watchThemeChanges() {
    try {
      const obs = new MutationObserver(() => renderBorder());
      obs.observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["data-theme", "data-show-border", "data-border-style", "class"],
      });
    } catch (_) { /* older browsers — static render, no live updates */ }
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
