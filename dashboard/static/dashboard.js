// Live dashboard: polls the Flask JSON endpoints, redraws, and sends the controls.
// Uses the helpers and the results drawing from results.js (loaded first).
"use strict";

const REFRESH_SECONDS = 3;          // live panels
const EXPERIMENT_REFRESH_SECONDS = 30;
const HEARTBEAT_STALE_S = 3;        // as MAX_HEARTBEAT_AGE_S in lb/strategies/ml_latency.py
const BUCKET_S = 5;                 // the dashboard_live_traffic view's bucket size

// Backends use palette slots 5-7 (strategies have 1-4). A backend keeps the
// slot it got when first seen: colour follows the entity, never its rank.
const BACKEND_SLOTS = [5, 6, 7];
const backendSlots = new Map();
function backendColor(name) {
  if (!backendSlots.has(name)) backendSlots.set(name, backendSlots.size);
  const i = backendSlots.get(name);
  return i < BACKEND_SLOTS.length ? css(`--s${BACKEND_SLOTS[i]}`) : css("--muted");
}

function stateCell(text, level) {
  const td = document.createElement("td");
  td.append(el("span", `state ${level}`, text));
  return td;
}

async function getJSON(url, options) {
  const resp = await fetch(url, {cache: "no-store", ...options});
  // An error page may be HTML (e.g. a proxy's 502), not JSON: report the status, not a parse error.
  const body = await resp.json().catch(() => null);
  if (!resp.ok || body === null) throw new Error((body && body.error) || `${url}: HTTP ${resp.status}`);
  return body;
}
const postJSON = (url, payload) => getJSON(url, {
  method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});

// One message per panel, so a success in one doesn't hide another's error.
const errors = {live: null, experiments: null, control: null};
function showError(panel, err) {
  errors[panel] = err ? err.message : null;
  const text = Object.values(errors).filter(Boolean).join(" · ");
  $("#error").textContent = text;
  $("#error").hidden = !text;
}

// --- live charts with event markers -------------------------------------------------

let events = [];       // [{t: epoch seconds, label}] from /api/events
let bucketTimes = [];  // epoch seconds at the start of each x label's bucket

// A dashed vertical line + label at the bucket where each control action happened:
// cause (a fault, a strategy switch) next to effect (the traffic moving).
const eventMarkers = {
  id: "eventMarkers",
  afterDatasetsDraw(chart) {
    const {ctx, chartArea, scales: {x}} = chart;
    if (!bucketTimes.length) return;
    ctx.save();
    ctx.strokeStyle = css("--ink-2");
    ctx.fillStyle = css("--ink");
    ctx.setLineDash([4, 3]);
    ctx.font = '10px system-ui, -apple-system, "Segoe UI", sans-serif';
    // Each label goes on the first text row where it doesn't overlap the previous
    // label's text (rowEnds = where each row's last label ends), so events close
    // together stack instead of printing on top of each other.
    // Events in the same bucket share one line and one label ("a, b, +2 more").
    const byBucket = new Map();
    for (const event of events) {
      const index = bucketTimes.findIndex(t => event.t >= t && event.t < t + BUCKET_S);
      if (index < 0) continue;   // before the window, or in the bucket that isn't complete yet
      if (!byBucket.has(index)) byBucket.set(index, []);
      byBucket.get(index).push(event.label);
    }
    const rowEnds = [];
    for (const [index, labels] of [...byBucket].sort((a, b) => a[0] - b[0])) {
      const label = labels.length > 2 ? `${labels.slice(-2).join(", ")}, +${labels.length - 2} more`
                                      : labels.join(", ");
      const px = x.getPixelForValue(index);
      ctx.beginPath();
      ctx.moveTo(px, chartArea.top);
      ctx.lineTo(px, chartArea.bottom);
      ctx.stroke();
      const width = ctx.measureText(label).width;
      const left = Math.max(chartArea.left, Math.min(px + 4, chartArea.right - width));
      let row = rowEnds.findIndex(end => end + 6 <= left);
      if (row < 0) {
        if (rowEnds.length >= 4) continue;   // no room left: the dashed line still marks it
        row = rowEnds.length;
      }
      rowEnds[row] = left + width;
      ctx.fillText(label, left, chartArea.top + 10 + 12 * row);
    }
    ctx.restore();
  },
};

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
    plugins: [eventMarkers],
  });
}

let rpsChart, p95Chart;

function updateLineCharts(points) {
  const buckets = [...new Set(points.map(p => p.t))].sort();
  const servers = [...new Set(points.map(p => p.server))].sort();
  bucketTimes = buckets.map(t => Date.parse(t) / 1000);
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

// --- backends table + controls ----------------------------------------------------------

let faults = {};   // address -> "none" | "slow" | "crash" | null (unreachable / no fault control)

function faultButtons(address) {
  const td = document.createElement("td");
  const current = faults[address];
  if (current == null) {
    td.append(el("span", "muted", "unavailable"));
    return td;
  }
  const group = el("div", "buttons");
  for (const [type, label] of [["slow", "Slow +150 ms"], ["crash", "Crash"], ["restore", "Restore"]]) {
    const button = el("button", type === "restore" ? "" : "danger", label);
    button.type = "button";
    // The active fault's own button is shown pressed; Restore is pointless when healthy.
    button.disabled = (type === current) || (type === "restore" && current === "none");
    button.setAttribute("aria-pressed", String(type === current));
    button.addEventListener("click", () => sendControl("fault", {backend: address, type, ms: 150}));
    group.append(button);
  }
  td.append(group);
  return td;
}

function updateBackends(live, summary) {
  const status = $("#lb-status");
  status.textContent = live.lb_running ? "LB running" : "LB not reachable";
  status.className = `pill state ${live.lb_running ? "good" : "critical"}`;
  const select = $("#strategy-select");
  select.disabled = !live.lb_running;
  if (live.strategy && document.activeElement !== select) select.value = live.strategy;
  $("#strategy-swatch").style.background = live.strategy ? strategyColor(live.strategy) : "transparent";
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
    const label = document.createElement("td");
    if (b) {
      const dot = el("span", "swatch");
      dot.style.background = backendColor(name);
      label.append(dot);
    }
    label.append(document.createTextNode(name));
    tr.append(label);
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
    tr.appendChild(b && live.lb_running ? faultButtons(name) : cell("–"));
    tbody.appendChild(tr);
  }
}

function updateLoad(load) {
  for (const button of document.querySelectorAll("#load-buttons button")) {
    const rate = Number(button.dataset.rate);
    const active = rate === 0 ? !load.running : load.running && load.rate === rate;
    button.setAttribute("aria-pressed", String(active));
  }
  $("#load-status").textContent = load.running
    ? `${load.rate} req/s · sent ${load.sent}, errors ${load.errors}${load.skipped ? `, skipped ${load.skipped}` : ""}`
    : "stopped";
}

async function sendControl(kind, payload) {
  try {
    await postJSON(`/api/control/${kind}`, payload);
    showError("control", null);
  } catch (err) {
    showError("control", err);
  }
  refreshLive();   // show the effect without waiting for the next tick
}

// A slow database or LB must not stack up overlapping polls: skip a tick instead.
let liveBusy = false;

async function refreshLive() {
  if (liveBusy) return;
  liveBusy = true;
  try {
    const [live, summary, traffic, load, recentEvents, currentFaults] = await Promise.all([
      getJSON("/api/backends"), getJSON("/api/summary"), getJSON("/api/traffic"),
      getJSON("/api/control/load"), getJSON("/api/events"), getJSON("/api/control/faults")]);
    events = recentEvents;
    faults = currentFaults;
    updateBackends(live, summary);
    updateLineCharts(traffic);
    updateLoad(load);
    showError("live", null);
    $("#updated").textContent = new Date().toLocaleTimeString([], {hour12: false});
  } catch (err) {
    showError("live", err);
  } finally {
    liveBusy = false;
  }
}

// --- experiment results ------------------------------------------------------------------

// The batch the viewer picked; null = let the server choose (newest complete batch).
// Not select.value: a freshly filled <select> reports its first option as chosen.
let chosenBatch = null;
// What is drawn now, to skip identical redraws: rebuilding the <select> every
// 30 s would close it under the viewer's cursor, and new charts drop any open tooltip.
let shownBatches = "", shownExperiments = "";

async function loadBatches() {
  const select = $("#batch");
  const batches = await getJSON("/api/batches");
  const key = JSON.stringify(batches);
  if (key === shownBatches) return;
  shownBatches = key;
  select.textContent = "";
  for (const b of batches) {
    const option = el("option", null, `${b.batch} (${b.runs} runs)`);
    option.value = b.batch;
    select.appendChild(option);
  }
}

async function refreshExperiments() {
  try {
    await loadBatches();
    const data = await getJSON("/api/experiments" + (chosenBatch ? `?batch=${encodeURIComponent(chosenBatch)}` : ""));
    if (data.batch) $("#batch").value = data.batch;
    // No batch in this database yet (e.g. a fresh clone): the committed final results are shown.
    $("#results-source").hidden = !data.source;
    if (data.source) $("#results-source").textContent = `No experiments in this database yet: showing the ` +
      `committed results of ${data.batch} (${data.source}). Run \`make experiments\` to measure your own.`;
    const key = JSON.stringify(data);
    if (key !== shownExperiments) {
      shownExperiments = key;
      drawResults(data);
    }
    showError("experiments", null);
  } catch (err) {
    showError("experiments", err);
  }
}

function start() {
  if (typeof Chart === "undefined") {  // loaded from a CDN: fails offline
    showError("live", new Error("Chart.js could not be loaded (offline?); the charts need it."));
    return;
  }
  applyChartDefaults();
  rpsChart = lineChart($("#rps-chart"), "requests / s");
  p95Chart = lineChart($("#p95-chart"), "ms");
  $("#interval").textContent = REFRESH_SECONDS;
  $("#batch").addEventListener("change", event => {
    chosenBatch = event.target.value;
    refreshExperiments();
  });
  $("#strategy-select").addEventListener("change", event => {
    sendControl("strategy", {strategy: event.target.value});
    event.target.blur();   // let the next refresh show the LB's actual strategy
  });
  for (const button of document.querySelectorAll("#load-buttons button")) {
    button.addEventListener("click", () => sendControl("load", {rate: Number(button.dataset.rate)}));
  }
  refreshLive();
  refreshExperiments();
  setInterval(refreshLive, REFRESH_SECONDS * 1000);
  setInterval(refreshExperiments, EXPERIMENT_REFRESH_SECONDS * 1000);
}

start();
