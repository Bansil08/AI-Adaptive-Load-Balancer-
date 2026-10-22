// Polls the Flask JSON endpoints and redraws. No framework: fetch + Chart.js.
"use strict";

const REFRESH_SECONDS = 3;          // live panels
const EXPERIMENT_REFRESH_SECONDS = 30;
const HEARTBEAT_STALE_S = 3;        // as MAX_HEARTBEAT_AGE_S in lb/strategies/ml_latency.py
const STRATEGIES = ["round_robin", "least_connections", "ml_latency", "bandit"];
const STRATEGY_LABELS = {round_robin: "Round robin", least_connections: "Least connections",
                         ml_latency: "ML latency", bandit: "Bandit"};
// Two-line tick labels: four full names don't fit under a ~250 px chart.
const TICK_LABELS = {round_robin: ["Round", "robin"], least_connections: ["Least", "conn."],
                     ml_latency: ["ML", "latency"], bandit: ["Bandit"]};

const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const $ = sel => document.querySelector(sel);

// Colour follows the entity, never its rank: a backend keeps the slot it got
// when first seen, and strategies always use slots 1-4 (as in the PNG charts).
const backendSlots = new Map();
function backendColor(name) {
  if (!backendSlots.has(name)) backendSlots.set(name, backendSlots.size + 1);
  const slot = backendSlots.get(name);
  return slot <= 8 ? css(`--s${slot}`) : css("--muted");
}
const strategyColor = name => css(`--s${STRATEGIES.indexOf(name) + 1}`) || css("--muted");

// Build table cells with textContent only: server names come from the network.
function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}
function stateCell(text, level) {
  const td = cell(text);
  const span = document.createElement("span");
  span.className = `state ${level}`;
  span.textContent = text;
  td.textContent = "";
  td.appendChild(span);
  return td;
}

const fmtMs = v => v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : v.toFixed(1);
// mean ± SD in ONE unit (the mean's), never "2.15 s ± 158.7".
const fmtSpread = m => m.mean >= 1000 ? `${(m.mean / 1000).toFixed(2)} ± ${(m.sd / 1000).toFixed(2)} s`
                                      : `${m.mean.toFixed(1)} ± ${m.sd.toFixed(1)}`;
const fmtPct = v => v == null ? "–" : `${(v * 100).toFixed(2)}%`;

async function getJSON(url) {
  const resp = await fetch(url, {cache: "no-store"});
  const body = await resp.json();
  if (!resp.ok) throw new Error(body.error || `${url}: HTTP ${resp.status}`);
  return body;
}

// --- Chart.js defaults from the CSS tokens -------------------------------------
function applyChartDefaults() {
  Chart.defaults.color = css("--ink-2");
  Chart.defaults.borderColor = css("--grid");
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  Chart.defaults.animation = false;          // a 3 s refresh shouldn't wobble
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
}

function lineChart(canvas, yTitle) {
  return new Chart(canvas, {
    type: "line",
    data: {labels: [], datasets: []},
    options: {
      maintainAspectRatio: false,
      interaction: {mode: "index", intersect: false},   // hover anywhere: all backends at that time
      elements: {line: {borderWidth: 2, tension: 0}, point: {radius: 0, hoverRadius: 4}},
      scales: {
        x: {grid: {display: false}, ticks: {maxTicksLimit: 6, maxRotation: 0}},
        y: {beginAtZero: true, title: {display: true, text: yTitle}},
      },
      plugins: {legend: {position: "bottom"}},
    },
  });
}

let rpsChart, p95Chart;

function updateLineCharts(points) {
  const buckets = [...new Set(points.map(p => p.t))].sort();
  const servers = [...new Set(points.map(p => p.server))].sort();
  const labels = buckets.map(t => new Date(t).toLocaleTimeString([], {hour12: false}));
  const lookup = new Map(points.map(p => [`${p.server}|${p.t}`, p]));
  for (const [chart, key, missing] of [[rpsChart, "rps", 0], [p95Chart, "p95_ms", null]]) {
    chart.data.labels = labels;
    // A bucket with no row had no requests: 0 req/s, but no p95 at all (a gap).
    chart.data.datasets = servers.map(server => ({
      label: server,
      data: buckets.map(t => lookup.get(`${server}|${t}`)?.[key] ?? missing),
      borderColor: backendColor(server),
      backgroundColor: backendColor(server),
    }));
    chart.update();
  }
}

function updateBackends(live, summary) {
  const status = $("#lb-status");
  status.textContent = live.lb_running ? "LB running" : "LB not reachable";
  status.className = `pill state ${live.lb_running ? "good" : "critical"}`;
  $("#strategy").textContent = live.strategy ? (STRATEGY_LABELS[live.strategy] || live.strategy)
    + (live.sticky_sessions ? " (sticky sessions)" : "") : "–";
  const fallbacks = $("#fallbacks");
  fallbacks.hidden = live.fallbacks == null;
  fallbacks.textContent = `Fallbacks to least connections: ${live.fallbacks}`;

  const recent = new Map(summary.map(r => [r.server, r]));
  const tbody = $("#backends tbody");
  tbody.textContent = "";
  const rows = live.backends.map(b => [b.address, b]);
  // Requests that no backend served (503 no backend, 429) get a row too.
  if (recent.has("(no backend)") && recent.get("(no backend)").requests > 0) rows.push(["(no backend)", null]);
  for (const [name, b] of rows) {
    const r = recent.get(name) || {};
    const tr = document.createElement("tr");
    tr.appendChild(cell(name));
    if (!b) {
      tr.append(cell("–"), cell("–"), cell("–", "num"));
    } else {
      const health = b.healthy == null ? [`last known: ${b.last_status}`, ""]   // LB not running
        : b.draining ? ["draining", "warning"] : b.healthy ? ["up", "good"] : ["down", "critical"];
      tr.appendChild(stateCell(...health));
      const circuit = {closed: "good", half_open: "warning", open: "critical"}[b.circuit];
      tr.appendChild(b.circuit ? stateCell(b.circuit.replace("_", "-"), circuit || "") : cell("–"));
      tr.appendChild(cell(b.in_flight ?? "–", "num"));
    }
    tr.append(cell(r.requests ?? 0, "num"), cell(r.retried ?? 0, "num"), cell(fmtPct(r.error_rate), "num"));
    const hb = b && b.heartbeat;
    // Heartbeats come every second; an old one describes a backend that has stopped.
    const hbText = !hb ? "–" : hb.age_s > HEARTBEAT_STALE_S ? `no heartbeat for ${hb.age_s.toFixed(0)} s`
      : `${hb.cpu_percent.toFixed(0)}% / ${hb.memory_percent.toFixed(0)}%`;
    tr.appendChild(cell(hbText, "num"));
    tbody.appendChild(tr);
  }
}

// A slow database or LB must not stack up overlapping polls: skip a tick instead.
let liveBusy = false;

async function refreshLive() {
  if (liveBusy) return;
  liveBusy = true;
  try {
    const [live, summary, traffic] = await Promise.all(
      [getJSON("/api/backends"), getJSON("/api/summary"), getJSON("/api/traffic")]);
    updateBackends(live, summary);
    updateLineCharts(traffic);
    $("#error").hidden = true;
    $("#updated").textContent = new Date().toLocaleTimeString([], {hour12: false});
  } catch (err) {
    $("#error").textContent = err.message;
    $("#error").hidden = false;
  } finally {
    liveBusy = false;
  }
}

// --- Experiment results ----------------------------------------------------------

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
      ctx.fillText(fmtMs(chart.data.datasets[0].data[i]), bar.x, bar.y - 3);
    });
    ctx.restore();
  },
};

const experimentCharts = [];

function drawExperimentCharts(cells) {
  experimentCharts.splice(0).forEach(c => c.destroy());
  const box = $("#experiment-charts");
  box.textContent = "";
  const scenarios = [...new Set(cells.map(c => c.scenario))];
  for (const scenario of scenarios) {
    const rows = cells.filter(c => c.scenario === scenario);
    const figure = document.createElement("figure");
    const caption = document.createElement("figcaption");
    caption.textContent = `${scenario.replaceAll("_", " ")}: p95 latency (ms)`;
    const wrap = document.createElement("div");
    wrap.className = "chart";
    const canvas = document.createElement("canvas");
    wrap.appendChild(canvas);
    figure.append(caption, wrap);
    box.appendChild(figure);
    experimentCharts.push(new Chart(canvas, {
      type: "bar",
      data: {
        labels: rows.map(r => TICK_LABELS[r.strategy] || r.strategy),
        datasets: [{
          data: rows.map(r => r.p95_latency_ms.mean),
          backgroundColor: rows.map(r => strategyColor(r.strategy)),
          borderColor: css("--surface"), borderWidth: 2,     // 2px gap between adjacent bars
          borderRadius: {topLeft: 4, topRight: 4}, borderSkipped: "bottom", maxBarThickness: 48,
        }],
      },
      options: {
        maintainAspectRatio: false,
        layout: {padding: {top: 18}},                         // room for the value labels
        scales: {x: {grid: {display: false}, ticks: {autoSkip: false, maxRotation: 0, font: {size: 10}}},
                 y: {beginAtZero: true}},
        plugins: {
          legend: {display: false},                            // the x labels name every bar
          tooltip: {callbacks: {label: item => {
            const r = rows[item.dataIndex].p95_latency_ms;
            return `p95 ${fmtSpread(r)} (min ${fmtMs(r.min)}, max ${fmtMs(r.max)})`;
          }}},
        },
      },
      plugins: [valueLabels],
    }));
  }
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
    tr.append(
      cell(STRATEGY_LABELS[c.strategy] || c.strategy),
      cell(c.runs, "num"),
      cell(fmtSpread(c.avg_latency_ms), "num"),
      cell(`${fmtSpread(c.p95_latency_ms)} [${fmtMs(c.p95_latency_ms.min)}–${fmtMs(c.p95_latency_ms.max)}]`, "num"),
      cell(fmtPct(c.error_rate.mean), "num"),
      cell(c.throughput_rps.mean.toFixed(1), "num"),
    );
    tbody.appendChild(tr);
  }
}

// The batch the viewer picked; null = let the server choose (newest complete batch).
// Not select.value: a freshly filled <select> reports its first option as chosen.
let chosenBatch = null;

async function loadBatches() {
  const select = $("#batch");
  const batches = await getJSON("/api/batches");
  select.textContent = "";
  for (const b of batches) {
    const option = document.createElement("option");
    option.value = b.batch;
    option.textContent = `${b.batch} (${b.runs} runs)`;
    select.appendChild(option);
  }
}

async function refreshExperiments() {
  try {
    await loadBatches();
    const select = $("#batch");
    const data = await getJSON("/api/experiments" + (chosenBatch ? `?batch=${encodeURIComponent(chosenBatch)}` : ""));
    if (data.batch) select.value = data.batch;
    drawExperimentCharts(data.cells);
    drawExperimentTable(data.cells);
  } catch (err) {
    $("#error").textContent = err.message;
    $("#error").hidden = false;
  }
}

function start() {
  applyChartDefaults();
  rpsChart = lineChart($("#rps-chart"), "requests / s");
  p95Chart = lineChart($("#p95-chart"), "ms");
  $("#interval").textContent = REFRESH_SECONDS;
  $("#batch").addEventListener("change", event => {
    chosenBatch = event.target.value;
    refreshExperiments();
  });
  refreshLive();
  refreshExperiments();
  setInterval(refreshLive, REFRESH_SECONDS * 1000);
  setInterval(refreshExperiments, EXPERIMENT_REFRESH_SECONDS * 1000);
}

start();
