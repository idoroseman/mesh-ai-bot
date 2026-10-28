// Repeater battery, voltage and temperature: one chart per measure on a shared time axis, one line per repeater.
(() => {
  const SOURCE = document.currentScript.dataset.source;
  const METRICS = [
    { key: "battery", label: "Battery", unit: "mV", digits: 0 },
    { key: "voltage", label: "Voltage", unit: "V", digits: 2 },
    { key: "temperature", label: "Temperature", unit: "°C", digits: 1 },
  ];
  const SLOTS = 8; // categorical palette size; more repeaters than this cannot be shown at once
  const DEFAULT_SELECTED = 3;
  const DIRECT_LABEL_LIMIT = 4;
  const DOT_LIMIT = 40; // draw markers when a line has this few points in range
  const TABLE_LIMIT = 300;
  const HEIGHT = 220;
  const SVG = "http://www.w3.org/2000/svg";

  const state = { series: [], days: 7, selected: [], slots: new Map() };

  function el(tag, attrs = {}, parent) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (parent) parent.appendChild(node);
    return node;
  }

  function color(name) {
    return `var(--series-${state.slots.get(name)})`;
  }

  function format(value, metric) {
    return value == null ? "–" : `${value.toFixed(metric.digits)} ${metric.unit}`;
  }

  function formatTime(t, withDate = true) {
    const date = new Date(t);
    const clock = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return withDate ? `${date.toLocaleDateString([], { day: "numeric", month: "short" })} ${clock}` : clock;
  }

  function domainStart() {
    return state.days ? Date.now() - state.days * 86400000 : -Infinity;
  }

  function inRange(points) {
    const start = domainStart();
    return points.filter((p) => p.t >= start);
  }

  function selectedSeries() {
    return state.selected
      .map((name) => state.series.find((s) => s.name === name))
      .filter(Boolean)
      .map((s) => ({ ...s, points: inRange(s.points) }));
  }

  // Colors follow the repeater: a slot is held while it stays selected, so others never repaint
  function select(name) {
    if (state.selected.length >= SLOTS) return;
    const used = new Set(state.slots.values());
    let slot = 1;
    while (used.has(slot)) slot++;
    state.slots.set(name, slot);
    state.selected.push(name);
  }

  function deselect(name) {
    state.selected = state.selected.filter((n) => n !== name);
    state.slots.delete(name);
  }

  function niceTicks(min, max, count) {
    if (min === max) {
      const pad = Math.abs(min) * 0.05 || 1;
      min -= pad;
      max += pad;
    }
    const raw = (max - min) / count;
    const power = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= raw);
    const ticks = [];
    for (let v = Math.floor(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(+v.toFixed(10));
    if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
    return ticks;
  }

  // A line breaks where readings stop for more than three usual probe intervals
  function gapLimit(points) {
    const gaps = points.slice(1).map((p, i) => p.t - points[i].t).sort((a, b) => a - b);
    return gaps.length ? Math.max(gaps[Math.floor(gaps.length / 2)] * 3, 600000) : Infinity;
  }

  function renderChart(metric, series) {
    const section = document.getElementById(`chart-${metric.key}`);
    section.querySelectorAll("svg, .tooltip").forEach((n) => n.remove());
    const width = section.clientWidth - 32;
    const lines = series
      .map((s) => ({ ...s, points: s.points.filter((p) => p[metric.key] != null) }))
      .filter((s) => s.points.length);
    // On a phone the line-end labels would squeeze the plot; the coloured chips above name the lines instead
    const labelled = lines.length <= DIRECT_LABEL_LIMIT && width >= 520;
    const margin = { top: 10, right: labelled ? Math.min(140, width * 0.3) : 12, bottom: 24, left: 52 };
    const svg = el("svg", { viewBox: `0 0 ${width} ${HEIGHT}`, role: "img", "aria-label": `${metric.label} by repeater` }, section);
    svg.style.height = `${HEIGHT}px`;

    if (!lines.length) {
      el("text", { x: width / 2, y: HEIGHT / 2, "text-anchor": "middle", class: "empty" }, svg).textContent =
        state.selected.length ? `No ${metric.label.toLowerCase()} readings in this range` : "Pick a repeater above";
      return;
    }

    const all = lines.flatMap((s) => s.points);
    const t0 = Math.max(domainStart(), Math.min(...all.map((p) => p.t)));
    const t1 = state.days ? Date.now() : Math.max(...all.map((p) => p.t));
    const yTicks = niceTicks(Math.min(...all.map((p) => p[metric.key])), Math.max(...all.map((p) => p[metric.key])), 4);
    const [y0, y1] = [yTicks[0], yTicks[yTicks.length - 1]];
    const plotW = width - margin.left - margin.right;
    const plotH = HEIGHT - margin.top - margin.bottom;
    const x = (t) => margin.left + ((t - t0) / (t1 - t0 || 1)) * plotW;
    const y = (v) => margin.top + plotH - ((v - y0) / (y1 - y0)) * plotH;

    const axis = el("g", { class: "axis" }, svg);
    const step = yTicks[1] - yTicks[0];
    // Enough decimals to tell the ticks apart: 0.25 steps need two, 0.5 steps one
    let decimals = 0;
    while (decimals < 4 && Math.abs(step * 10 ** decimals - Math.round(step * 10 ** decimals)) > 1e-6) decimals++;
    for (const v of yTicks) {
      el("line", { class: v === y0 ? "baseline" : "gridline", x1: margin.left, x2: margin.left + plotW, y1: y(v), y2: y(v) }, axis);
      el("text", { x: margin.left - 8, y: y(v) + 4, "text-anchor": "end" }, axis).textContent = v.toFixed(decimals);
    }
    const span = t1 - t0;
    const xCount = Math.max(2, Math.floor(plotW / 110));
    for (let i = 0; i <= xCount; i++) {
      const t = t0 + (span * i) / xCount;
      el("text", { x: x(t), y: HEIGHT - 6, "text-anchor": i === 0 ? "start" : i === xCount ? "end" : "middle" }, axis).textContent =
        span <= 86400000 * 1.5 ? formatTime(t, false) : new Date(t).toLocaleDateString([], { day: "numeric", month: "short" });
    }

    const labels = [];
    for (const s of lines) {
      const limit = gapLimit(s.points);
      let d = "";
      s.points.forEach((p, i) => {
        const jump = i === 0 || p.t - s.points[i - 1].t > limit;
        d += `${jump ? "M" : "L"}${x(p.t).toFixed(1)},${y(p[metric.key]).toFixed(1)}`;
      });
      el("path", { class: "series", d }, svg).style.stroke = color(s.name);
      if (s.points.length <= DOT_LIMIT) {
        for (const p of s.points) el("circle", { class: "dot", cx: x(p.t), cy: y(p[metric.key]), r: 4 }, svg).style.fill = color(s.name);
      }
      const last = s.points[s.points.length - 1];
      labels.push({ name: s.name, x: x(last.t), y: y(last[metric.key]) });
    }

    if (labelled) {
      // Push end labels apart so neighbouring lines stay readable
      labels.sort((a, b) => a.y - b.y);
      for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 13);
      for (const label of labels) {
        const text = el("text", { class: "label", x: margin.left + plotW + 8, y: label.y + 4 }, svg);
        text.textContent = label.name.length > 20 ? `${label.name.slice(0, 19)}…` : label.name;
      }
    }

    // Hover: a crosshair snaps to the nearest reading and the tooltip lists every repeater at that time
    const hover = el("g", { visibility: "hidden" }, svg);
    const hair = el("line", { class: "crosshair", y1: margin.top, y2: margin.top + plotH }, hover);
    const markers = el("g", {}, hover);
    const tip = document.createElement("div");
    tip.className = "tooltip";
    tip.hidden = true;
    section.appendChild(tip);
    const hit = el("rect", { x: margin.left, y: margin.top, width: plotW, height: plotH, fill: "transparent" }, svg);

    function nearest(points, t) {
      let best = points[0];
      for (const p of points) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
      return best;
    }

    hit.addEventListener("pointermove", (event) => {
      const box = svg.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * width;
      const t = t0 + ((px - margin.left) / plotW) * span;
      const snapped = nearest(all, t).t;
      hair.setAttribute("x1", x(snapped));
      hair.setAttribute("x2", x(snapped));
      markers.replaceChildren();
      tip.replaceChildren();
      const when = document.createElement("div");
      when.className = "when";
      when.textContent = formatTime(snapped);
      tip.appendChild(when);
      for (const s of lines) {
        const p = nearest(s.points, snapped);
        const close = Math.abs(p.t - snapped) <= gapLimit(s.points) / 3;
        if (close) el("circle", { class: "dot", cx: x(p.t), cy: y(p[metric.key]), r: 5 }, markers).style.fill = color(s.name);
        const row = document.createElement("div");
        const swatch = document.createElement("i");
        swatch.style.background = color(s.name);
        const text = document.createElement("span");
        text.textContent = `${s.name}: ${close ? format(p[metric.key], metric) : "–"}`;
        row.append(swatch, text);
        tip.appendChild(row);
      }
      hover.setAttribute("visibility", "visible");
      tip.hidden = false;
      const left = 16 + (x(snapped) / width) * (section.clientWidth - 32);
      tip.style.top = "36px";
      tip.style.left = left + tip.offsetWidth + 12 > section.clientWidth ? `${left - tip.offsetWidth - 12}px` : `${left + 12}px`;
    });
    hit.addEventListener("pointerleave", () => {
      hover.setAttribute("visibility", "hidden");
      tip.hidden = true;
    });
  }

  function renderTable(series) {
    const body = document.querySelector("#table-view tbody");
    const rows = series
      .flatMap((s) => s.points.map((p) => ({ ...p, name: s.name })))
      .sort((a, b) => b.t - a.t)
      .slice(0, TABLE_LIMIT);
    body.replaceChildren(
      ...rows.map((p) => {
        const tr = document.createElement("tr");
        for (const value of [formatTime(p.t), p.name, ...METRICS.map((m) => (p[m.key] == null ? "" : p[m.key].toFixed(m.digits)))]) {
          const td = document.createElement("td");
          td.textContent = value;
          tr.appendChild(td);
        }
        return tr;
      }),
    );
  }

  function renderControls() {
    for (const button of document.querySelectorAll("#range button")) {
      button.setAttribute("aria-pressed", String(Number(button.dataset.days) === state.days));
    }
    const chips = document.getElementById("repeaters");
    chips.replaceChildren(
      ...state.series.map((s) => {
        const on = state.selected.includes(s.name);
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.setAttribute("aria-pressed", String(on));
        chip.disabled = !on && state.selected.length >= SLOTS;
        const swatch = document.createElement("i");
        if (on) swatch.style.background = color(s.name);
        const label = document.createElement("span");
        label.textContent = s.name;
        chip.append(swatch, label);
        chip.addEventListener("click", () => {
          on ? deselect(s.name) : select(s.name);
          render();
        });
        return chip;
      }),
    );
  }

  function render() {
    renderControls();
    const series = selectedSeries();
    for (const metric of METRICS) renderChart(metric, series);
    renderTable(series);
  }

  document.getElementById("range").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    state.days = Number(button.dataset.days);
    render();
  });

  let resizeTimer;
  new ResizeObserver(() => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 100);
  }).observe(document.querySelector("main"));

  fetch(SOURCE)
    .then((response) => response.json())
    .then((series) => {
      state.series = series;
      // Start with the repeaters that reported most in the chosen range
      [...series]
        .sort((a, b) => inRange(b.points).length - inRange(a.points).length)
        .slice(0, DEFAULT_SELECTED)
        .forEach((s) => select(s.name));
      render();
    })
    .catch((error) => {
      document.getElementById("chart-battery").append(`Could not load the readings: ${error}`);
    });
})();
