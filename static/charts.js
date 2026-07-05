"use strict";

// Tiny dependency-free SVG chart helpers shared by the dashboard pages.
// Each renderer replaces the innerHTML of the given container element.
// Everything is scaled to a nominal viewBox and stretched to the container
// width via width:100%, so charts stay responsive without measuring layout.

const Charts = (() => {
  const NS = "http://www.w3.org/2000/svg";

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtBytes(n, digits) {
    n = Number(n) || 0;
    const neg = n < 0 ? "-" : "";
    n = Math.abs(n);
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let i = 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    const d = digits == null ? (i >= 3 ? 2 : (i === 0 ? 0 : 1)) : digits;
    return neg + n.toFixed(d) + " " + units[i];
  }

  function fmtBps(n) {
    // bytes/sec -> human "/s". Show bits? Keep bytes for consistency with capacity.
    return fmtBytes(n, n >= 1e9 ? 2 : 1) + "/s";
  }

  // --- Donut / pie ---------------------------------------------------------
  // segments: [{ value, color, label }]. Draws a stacked ring.
  function donut(el, segments, opts = {}) {
    const size = opts.size || 220;
    const stroke = opts.stroke || 34;
    const r = (size - stroke) / 2;
    const cx = size / 2, cy = size / 2;
    const C = 2 * Math.PI * r;
    const total = segments.reduce((a, s) => a + Math.max(0, Number(s.value) || 0), 0);

    let parts = "";
    // Track background
    parts += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${opts.trackColor || "#1b2530"}" stroke-width="${stroke}"/>`;
    if (total > 0) {
      let offset = 0;
      for (const s of segments) {
        const v = Math.max(0, Number(s.value) || 0);
        if (v <= 0) continue;
        const frac = v / total;
        const len = frac * C;
        parts += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${s.color}" stroke-width="${stroke}" ` +
          `stroke-dasharray="${len} ${C - len}" stroke-dashoffset="${-offset}" ` +
          `transform="rotate(-90 ${cx} ${cy})"><title>${esc(s.label || "")}</title></circle>`;
        offset += len;
      }
    }
    const centerTop = opts.centerTop != null ? `<div class="donut-center-top">${esc(opts.centerTop)}</div>` : "";
    const centerSub = opts.centerBottom != null ? `<div class="donut-center-sub">${esc(opts.centerBottom)}</div>` : "";
    el.innerHTML =
      `<div class="donut-wrap" style="max-width:${size}px">` +
      `<svg viewBox="0 0 ${size} ${size}" class="donut-svg" role="img">${parts}</svg>` +
      `<div class="donut-center">${centerTop}${centerSub}</div>` +
      `</div>`;
  }

  // --- Filled pie (old-Drobo style) ---------------------------------------
  // Full pie with solid wedges; boundaries between slices are clean straight
  // radial lines (no donut hole, no seam). segments: [{ value, color, label }].
  function pie(el, segments, opts = {}) {
    const size = opts.size || 200;
    const cx = size / 2, cy = size / 2;
    const r = size / 2 - (opts.pad != null ? opts.pad : 1);
    const edge = opts.edge || "#0f151b";
    const edgeW = opts.edgeWidth != null ? opts.edgeWidth : 2;
    const active = segments
      .map((s) => ({ ...s, v: Math.max(0, Number(s.value) || 0) }))
      .filter((s) => s.v > 0);
    const total = active.reduce((a, s) => a + s.v, 0);

    let parts = "";
    if (total <= 0) {
      parts = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${opts.trackColor || "#1b2530"}"/>`;
    } else if (active.length === 1) {
      // Single slice: a plain circle, so there's no stray radial seam line.
      const s = active[0];
      parts = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${s.color}" stroke="${edge}" stroke-width="${edgeW}"><title>${esc(s.label || "")}</title></circle>`;
    } else {
      let a0 = -Math.PI / 2; // start at 12 o'clock
      for (const s of active) {
        const a1 = a0 + (s.v / total) * 2 * Math.PI;
        const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
        const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
        const large = a1 - a0 > Math.PI ? 1 : 0;
        parts +=
          `<path d="M${cx},${cy} L${x0.toFixed(2)},${y0.toFixed(2)} ` +
          `A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)} Z" ` +
          `fill="${s.color}" stroke="${edge}" stroke-width="${edgeW}" stroke-linejoin="round">` +
          `<title>${esc(s.label || "")}</title></path>`;
        a0 = a1;
      }
    }
    el.innerHTML =
      `<div class="pie-wrap" style="max-width:${size}px">` +
      `<svg viewBox="0 0 ${size} ${size}" class="pie-svg" role="img">${parts}</svg>` +
      `</div>`;
  }

  // --- Horizontal stacked bar ---------------------------------------------
  // segments: [{ value, color, label }]
  function stackedBar(el, segments) {
    const total = segments.reduce((a, s) => a + Math.max(0, Number(s.value) || 0), 0) || 1;
    let x = 0, cells = "";
    for (const s of segments) {
      const v = Math.max(0, Number(s.value) || 0);
      const w = (v / total) * 100;
      if (w <= 0) continue;
      cells += `<div class="hbar-cell" style="width:${w}%;background:${s.color}" title="${esc(s.label || "")}"></div>`;
      x += w;
    }
    el.innerHTML = `<div class="hbar">${cells}</div>`;
  }

  // --- Area / line time series --------------------------------------------
  // series: [{ values: [{t, v}], color, fill, label }]
  function area(el, series, opts = {}) {
    const W = 700, H = opts.height || 160;
    const padL = 4, padR = 4, padT = 8, padB = 16;
    const plotW = W - padL - padR, plotH = H - padT - padB;

    let allV = [], tmin = Infinity, tmax = -Infinity;
    for (const s of series) {
      for (const p of s.values) {
        allV.push(p.v);
        if (p.t < tmin) tmin = p.t;
        if (p.t > tmax) tmax = p.t;
      }
    }
    if (!allV.length) {
      el.innerHTML = `<div class="chart-empty muted">${esc(opts.emptyText || "No data yet — collecting…")}</div>`;
      return;
    }
    let yMax = opts.yMax != null ? opts.yMax : Math.max.apply(null, allV);
    if (yMax <= 0) yMax = 1;
    yMax *= 1.1;
    const tspan = (tmax - tmin) || 1;

    const xOf = (t) => padL + ((t - tmin) / tspan) * plotW;
    const yOf = (v) => padT + plotH - (Math.max(0, v) / yMax) * plotH;

    let paths = "";
    // gridlines (25/50/75%)
    for (const g of [0.25, 0.5, 0.75, 1]) {
      const y = padT + plotH - g * plotH;
      paths += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="#202832" stroke-width="1"/>`;
    }
    for (const s of series) {
      if (!s.values.length) continue;
      const pts = s.values.map((p) => `${xOf(p.t).toFixed(1)},${yOf(p.v).toFixed(1)}`);
      const line = "M" + pts.join(" L");
      if (s.fill !== false) {
        const areaPath = line + ` L${xOf(s.values[s.values.length - 1].t).toFixed(1)},${(padT + plotH).toFixed(1)}` +
          ` L${xOf(s.values[0].t).toFixed(1)},${(padT + plotH).toFixed(1)} Z`;
        paths += `<path d="${areaPath}" fill="${s.fill || s.color}" fill-opacity="0.16"/>`;
      }
      paths += `<path d="${line}" fill="none" stroke="${s.color}" stroke-width="2" ` +
        `stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`;
    }
    const top = opts.fmtY ? opts.fmtY(yMax / 1.1) : (yMax / 1.1).toFixed(0);
    el.innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" class="ts-svg" preserveAspectRatio="none" role="img">${paths}</svg>` +
      `<div class="chart-axis"><span>${esc(top)}</span><span>0</span></div>`;
  }

  // --- Vertical bar chart (e.g. per-day) -----------------------------------
  // bars: [{ label, value, sub }]. Supports negative values (rendered downward).
  function bars(el, data, opts = {}) {
    if (!data.length) {
      el.innerHTML = `<div class="chart-empty muted">${esc(opts.emptyText || "No data yet.")}</div>`;
      return;
    }
    const posColor = opts.posColor || "#57c23a";
    const negColor = opts.negColor || "#3a6ea5";
    const maxAbs = Math.max(1, ...data.map((d) => Math.abs(Number(d.value) || 0)));
    let cols = "";
    for (const d of data) {
      const v = Number(d.value) || 0;
      const h = (Math.abs(v) / maxAbs) * 100;
      const cls = v < 0 ? "neg" : "pos";
      const color = v < 0 ? negColor : posColor;
      const title = (opts.fmtV ? opts.fmtV(v) : v) + (d.sub ? " · " + d.sub : "");
      cols +=
        `<div class="bar-col" title="${esc(d.label + ": " + title)}">` +
        `<div class="bar-track"><div class="bar-fill ${cls}" style="height:${h.toFixed(1)}%;background:${color}"></div></div>` +
        `<div class="bar-label">${esc(d.label)}</div>` +
        `</div>`;
    }
    el.innerHTML = `<div class="bar-chart">${cols}</div>`;
  }

  return { donut, pie, stackedBar, area, bars, fmtBytes, fmtBps, esc };
})();
