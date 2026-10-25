// Experiment results: headline banner, winner cards, per-scenario charts and the table.
// Shared by the live dashboard (dashboard.js) and the static results page in docs/
// (copied there by experiments/export_static.py), so both draw the same thing.
"use strict";

const STRATEGIES = ["round_robin", "least_connections", "ml_latency", "bandit"];
const STRATEGY_LABELS = {round_robin: "Round robin", least_connections: "Least connections",
                         ml_latency: "ML latency", bandit: "Bandit"};
// Two-line tick labels: four full names don't fit under a ~250 px chart.
const TICK_LABELS = {round_robin: ["Round", "robin"], least_connections: ["Least", "conn."],
                     ml_latency: ["ML", "latency"], bandit: ["Bandit"]};

const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const $ = sel => document.querySelector(sel);

// Strategies always use palette slots 1-4 (the same colours as the PNG charts);
// backends use slots 5-7 (dashboard.js), so a colour never means both.
const strategyColor = name => STRATEGIES.includes(name) ? css(`--s${STRATEGIES.indexOf(name) + 1}`) : css("--muted");

// Build cells with textContent only: names come from the network.
function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
// A strategy's name with its colour swatch: identity is never colour alone.
function strategyBadge(name) {
  const badge = el("span", "badge");
  const dot = el("span", "swatch");
  dot.style.background = strategyColor(name);
  badge.append(dot, document.createTextNode(STRATEGY_LABELS[name] || name));
  return badge;
}

const fmtMs = v => v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : v.toFixed(1);
// mean ± SD in ONE unit (the mean's), never "2.15 s ± 158.7".
const fmtSpread = m => m.mean >= 1000 ? `${(m.mean / 1000).toFixed(2)} ± ${(m.sd / 1000).toFixed(2)} s`
                                      : `${m.mean.toFixed(1)} ± ${m.sd.toFixed(1)}`;
const fmtPct = v => v == null ? "–" : `${(v * 100).toFixed(2)}%`;
// With its unit: "31.3 ms" or "3.84 s" (fmtMs alone is for columns already headed "ms").
const fmtLatency = v => v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(1)} ms`;
const fmtChange = v => v == null ? "" : Math.abs(v) < 0.005 ? "no change"
  : `${v < 0 ? "−" : "+"}${Math.abs(v * 100).toFixed(0)}%`;
const MISSING = "—";   // a percentile this batch never measured (older batches have only p95)

// --- percentile choice ---------------------------------------------------------------

const PERCENTILES = ["p50", "p95", "p99"];
// A viewer's choice is remembered in localStorage, which can be missing or throw
// (private windows, blocked storage): then the page simply starts at p95.
function savedChoice(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return PERCENTILES.includes(value) ? value : fallback;
  } catch {
    return fallback;
  }
}
function saveChoice(key, value) {
  try { localStorage.setItem(key, value); } catch { /* not remembered: fine */ }
}

// A segmented p50 | p95 | p99 control; onChange(p) is called with the new choice.
function percentileToggle(box, current, onChange) {
  box.textContent = "";
  box.className = "segmented";
  box.setAttribute("role", "group");
  for (const p of PERCENTILES) {
    const button = el("button", null, p);
    button.type = "button";
    button.setAttribute("aria-pressed", String(p === current));
    button.addEventListener("click", () => {
      for (const b of box.children) b.setAttribute("aria-pressed", String(b === button));
      onChange(p);
    });
    box.append(button);
  }
}

let resultsPercentile = savedChoice("adaptive-lb.results-percentile", "p95");
const latencyKey = p => `${p}_latency_ms`;

function applyChartDefaults() {
  Chart.defaults.color = css("--ink-2");
  Chart.defaults.borderColor = css("--grid");
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.animation = false;          // a refresh shouldn't wobble
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
}

// --- headline + cards ---------------------------------------------------------------

// The headline and cards for the chosen percentile (dashboard/summary.py computes all three).
function summaryFor(data, p) {
  if (data.by_percentile && data.by_percentile[p]) return data.by_percentile[p];
  return p === "p95" ? {headline: data.headline, cards: data.cards || []} : {headline: null, cards: []};
}

function drawHeadline(data) {
  const banner = $("#headline");
  if (!banner) return;
  const {headline, cards} = summaryFor(data, resultsPercentile);
  banner.hidden = !headline;
  if (headline) $("#headline-text").textContent = headline.text;

  const box = $("#winner-cards");
  box.textContent = "";
  for (const card of cards) {
    const node = el("div", "card");
    node.append(el("div", "card-scenario", card.label));
    const who = el("div", "card-winner");
    if (card.clear_winner) {
      who.append(strategyBadge(card.winners[0]));
    } else {
      who.append(el("span", "tie", "No clear winner: "));
      card.winners.forEach((w, i) => {
        if (i) who.append(document.createTextNode(", "));
        who.append(strategyBadge(w));
      });
      who.append(el("span", "muted", " within noise"));
    }
    node.append(who);
    const numbers = el("div", "card-numbers");
    numbers.append(el("strong", null, `${card.percentile} ${fmtLatency(card.value_ms)}`));
    if (card.change_vs_round_robin != null && card.winners[0] !== "round_robin") {
      numbers.append(el("span", "muted",
        ` vs ${fmtLatency(card.round_robin_ms)} round robin (${fmtChange(card.change_vs_round_robin)})`));
    }
    node.append(numbers);
    box.append(node);
  }
}

// --- per-scenario charts ---------------------------------------------------------------

// Draws each bar's value above it: identity and value never depend on colour alone.
const valueLabels = {
  id: "valueLabels",
  afterDatasetsDraw(chart) {
    const {ctx} = chart;
    ctx.save();
    ctx.fillStyle = css("--ink");
    ctx.font = '11px system-ui, -apple-system, "Segoe UI", sans-serif';
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    chart.getDatasetMeta(0).data.forEach((bar, i) => {
      const value = chart.data.datasets[0].data[i];
      if (value != null) ctx.fillText(fmtMs(value), bar.x, bar.y - 3);
    });
    ctx.restore();
  },
};

const experimentCharts = [];
const logScale = new Set();   // scenarios the viewer switched to a log axis (kept across redraws)

function drawExperimentCharts(data) {
  const cells = data.cells;
  const p = resultsPercentile, key = latencyKey(p);
  const notes = Object.fromEntries((data.cards || []).map(c => [c.scenario, c.note]));
  experimentCharts.splice(0).forEach(c => c.destroy());
  const box = $("#experiment-charts");
  box.textContent = "";
  for (const scenario of [...new Set(cells.map(c => c.scenario))]) {
    const rows = cells.filter(c => c.scenario === scenario);
    const values = rows.map(r => r[key] ? r[key].mean : null);
    const measured = values.filter(v => v != null);
    // Only where one bar dwarfs the rest (round robin's 2-4 s backlogs) is a log axis useful.
    const skewed = measured.length > 1 && Math.max(...measured) / Math.max(Math.min(...measured), 1e-9) > 10;
    // A log axis chosen at one percentile only applies while there is a toggle to undo it:
    // switching to a percentile where this chart isn't skewed shows it linear again.
    const log = skewed && logScale.has(scenario);
    const figure = el("figure");
    const head = el("div", "figure-head");
    const caption = el("figcaption", null, `${scenario.replaceAll("_", " ")}: ${p} latency (ms)`);
    caption.append(el("span", "hint", "lower is better"));
    head.append(caption);
    const wrap = el("div", "chart");
    const canvas = el("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `${scenario.replaceAll("_", " ")}: ` +
      rows.map(r => `${STRATEGY_LABELS[r.strategy] || r.strategy} ` +
                    (r[key] ? `${fmtMs(r[key].mean)} ms` : "not measured")).join(", "));
    wrap.append(canvas);
    figure.append(head, wrap);
    if (notes[scenario]) figure.append(el("p", "note", notes[scenario]));
    box.append(figure);

    const chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels: rows.map(r => TICK_LABELS[r.strategy] || r.strategy),
        datasets: [{
          data: values,
          backgroundColor: rows.map(r => strategyColor(r.strategy)),
          borderColor: css("--surface"), borderWidth: 2,     // 2px gap between adjacent bars
          borderRadius: {topLeft: 4, topRight: 4}, borderSkipped: "bottom", maxBarThickness: 48,
        }],
      },
      options: {
        maintainAspectRatio: false,
        layout: {padding: {top: 18}},                         // room for the value labels
        scales: {x: {grid: {display: false}, ticks: {autoSkip: false, maxRotation: 0, font: {size: 10}}},
                 y: log ? {type: "logarithmic"} : {beginAtZero: true}},
        plugins: {
          legend: {display: false},                            // the x labels name every bar
          tooltip: {callbacks: {label: item => {
            const r = rows[item.dataIndex][key];
            return `${p} ${fmtSpread(r)} (min ${fmtMs(r.min)}, max ${fmtMs(r.max)})`;
          }}},
        },
      },
      plugins: [valueLabels],
    });
    experimentCharts.push(chart);

    if (skewed) {
      const toggle = el("button", "toggle", log ? "Linear scale" : "Log scale");
      toggle.type = "button";
      toggle.title = "Switch the y axis between linear and logarithmic";
      toggle.addEventListener("click", () => {
        const toLog = !logScale.has(scenario);
        toLog ? logScale.add(scenario) : logScale.delete(scenario);
        chart.options.scales.y = toLog ? {type: "logarithmic"} : {beginAtZero: true};
        chart.update();
        toggle.textContent = toLog ? "Linear scale" : "Log scale";
      });
      head.append(toggle);
    }
  }
}

// mean ± SD, with the min–max range on a smaller second line; "—" if not measured.
function spreadCell(m) {
  const td = cell(null, "num");
  if (!m) {
    td.textContent = MISSING;
    td.title = "not measured in this batch (older batches have only p95)";
    return td;
  }
  td.append(el("div", null, fmtSpread(m)), el("div", "range", `${fmtMs(m.min)}–${fmtMs(m.max)}`));
  return td;
}

function drawExperimentTable(cells) {
  const tbody = $("#experiments tbody");
  tbody.textContent = "";
  let previous = null;
  for (const c of cells) {
    const tr = document.createElement("tr");
    if (c.scenario !== previous && previous !== null) tr.className = "group-start";
    tr.appendChild(cell(c.scenario === previous ? "" : c.scenario.replaceAll("_", " ")));
    previous = c.scenario;
    const name = document.createElement("td");
    name.append(strategyBadge(c.strategy));
    tr.append(
      name,
      cell(c.runs, "num"),
      ...PERCENTILES.map(p => spreadCell(c[latencyKey(p)])),
      cell(fmtSpread(c.avg_latency_ms), "num"),
      cell(fmtPct(c.error_rate.mean), "num"),
      cell(c.throughput_rps.mean.toFixed(1), "num"),
    );
    tbody.appendChild(tr);
  }
}

function drawResults(data) {
  // One toggle for the headline, the cards and all four charts (the table shows all three).
  const toggle = $("#results-percentile");
  if (toggle) percentileToggle(toggle, resultsPercentile, p => {
    resultsPercentile = p;
    saveChoice("adaptive-lb.results-percentile", p);
    drawHeadline(data);
    drawExperimentCharts(data);
  });
  drawHeadline(data);
  drawExperimentCharts(data);
  drawExperimentTable(data.cells);
}
