"""Plot comparison graphs from run_results.

    python -m experiments.plot_results                 # the latest batch of runs
    python -m experiments.plot_results --batch baseline-20261022T030000Z

Writes PNGs to experiments/results/:
  - baseline_bars.png: per scenario (columns), p95 latency, average latency and
    error rate (rows) for each strategy; p95 bars show the min-max range over
    the repeats. Each panel has its own scale: the scenarios differ by orders
    of magnitude, and a panel compares strategies within one scenario.
  - timeline_sudden_slowdown.png, timeline_server_crash.png: p95 latency per
    second (top) and the affected backend's share of requests or the errors
    per second (bottom), pooled over the repeats, with the event marked.
    Two stacked charts on a shared time axis, never one chart with two y-axes.

All numbers come from the database (run_results and the requests tagged with
each run_id), so the charts can be redrawn at any time without re-running.
"""

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # files only, no window
import matplotlib.pyplot as plt  # noqa: E402
import psycopg  # noqa: E402

from experiments.benchmark_concurrency import RESULTS_DIR  # noqa: E402
from experiments.scenarios import SCENARIOS  # noqa: E402
from lb.config import database_url  # noqa: E402

# Colour follows the strategy, never its rank, and stays the same in every chart
# and phase. Fixed categorical order, validated for colour-vision deficiency
# (adjacent pairs) with the dataviz validator; slots 3-4 (the Phase 6 ML
# strategies) are below 3:1 contrast, so every bar also carries a value label.
STRATEGY_COLORS = {
    "round_robin": "#2a78d6",
    "least_connections": "#eb6834",
    "ml_latency": "#1baf7a",
    "bandit": "#eda100",
}
LABELS = {"round_robin": "Round robin", "least_connections": "Least connections",
          "ml_latency": "ML latency", "bandit": "Bandit"}
SURFACE, INK, INK_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

BARS_SQL = """
SELECT e.scenario, e.strategy,
       avg(rr.p95_latency_ms), min(rr.p95_latency_ms), max(rr.p95_latency_ms),
       avg(rr.avg_latency_ms), avg(rr.error_rate)
FROM run_results rr JOIN experiment_runs e ON e.id = rr.run_id
WHERE e.notes::jsonb ->> 'batch' = %s
GROUP BY e.scenario, e.strategy
"""

# Per second since the load started, pooled over all repeats of a scenario.
TIMELINE_SQL = """
SELECT e.strategy,
       floor(extract(epoch FROM r.received_at - e.started_at))::int AS second,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY r.response_time_ms)
           FILTER (WHERE r.status_code < 500)                        AS p95_ms,
       count(*) FILTER (WHERE r.status_code >= 500)::float8
           / count(DISTINCT e.id)                                    AS errors_per_run,
       count(*) FILTER (WHERE s.port = (e.notes::jsonb -> 'backends' ->> %(backend)s)::int)::float8
           / count(*)                                                AS share
FROM requests r
JOIN experiment_runs e ON e.id = r.run_id
LEFT JOIN servers s ON s.id = r.server_id
WHERE e.notes::jsonb ->> 'batch' = %(batch)s AND e.scenario = %(scenario)s
  AND r.received_at >= e.started_at
GROUP BY e.strategy, second
ORDER BY e.strategy, second
"""


def latest_batch(conn) -> str:
    row = conn.execute("SELECT notes::jsonb ->> 'batch' FROM experiment_runs e JOIN run_results rr ON rr.run_id = e.id"
                       " WHERE notes IS NOT NULL ORDER BY e.id DESC LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("no experiment results in the database yet; run python -m experiments.run_experiments")
    return row[0]


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def fmt(value: float, unit: str) -> str:
    if unit == "%":
        return f"{value:.2%}"
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f}"


def plot_bars(rows, strategies, scenarios, path: Path) -> Path:
    data = {(sc, st): vals for sc, st, *vals in rows}
    metrics = [("p95 latency (ms)", 0, "ms"), ("Average latency (ms)", 3, "ms"), ("Error rate", 4, "%")]
    fig, axes = plt.subplots(len(metrics), len(scenarios), figsize=(3.2 * len(scenarios), 8.4),
                             facecolor=SURFACE, squeeze=False)
    for col, scenario in enumerate(scenarios):
        for row, (title, index, unit) in enumerate(metrics):
            ax = axes[row][col]
            style(ax)
            present = [st for st in strategies if (scenario, st) in data]
            values = [data[(scenario, st)][index] for st in present]
            x = range(len(present))
            # edgecolor = surface draws the 2px gap between adjacent bars
            bars = ax.bar(x, values, width=0.62, color=[STRATEGY_COLORS[s] for s in present],
                          edgecolor=SURFACE, linewidth=2)
            if index == 0:  # min-max over the repeats
                lows = [v - data[(scenario, st)][1] for v, st in zip(values, present)]
                highs = [data[(scenario, st)][2] - v for v, st in zip(values, present)]
                ax.errorbar(x, values, yerr=[lows, highs], fmt="none", ecolor=INK_2, elinewidth=1, capsize=3)
            top = max([data[(scenario, st)][2] if index == 0 else v for v, st in zip(values, present)] or [1])
            # All zero (e.g. no errors at all): a small fixed range instead of 0-100%.
            ax.set_ylim(0, top * 1.22 if top > 0 else (0.01 if unit == "%" else 1))
            for bar, value, st in zip(bars, values, present):  # direct labels: identity is never colour alone
                anchor = data[(scenario, st)][2] if index == 0 else value  # above the whisker, not on it
                ax.annotate(fmt(value, unit), (bar.get_x() + bar.get_width() / 2, anchor),
                            xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                            fontsize=9, color=INK)
            ax.set_xticks(list(x), [LABELS[s] for s in present], fontsize=9, color=INK_2)
            if unit == "%":
                ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=2))
            if col == 0:
                ax.set_ylabel(title, color=INK_2, fontsize=10)
            if row == 0:
                ax.set_title(scenario.replace("_", " "), color=INK, fontsize=11, pad=10)
    fig.suptitle("Round robin vs least connections, per scenario (mean of repeats; p95 bars show min–max)",
                 color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return path


def plot_timeline(rows, scenario_name: str, lower: str, path: Path, duration: float) -> Path:
    scenario = SCENARIOS[scenario_name].scaled(duration)  # the batch may have used a shorter duration
    by_strategy: dict[str, list] = {}
    for strategy, second, p95, errors, share in rows:
        by_strategy.setdefault(strategy, []).append((second, p95, errors, share))
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True, facecolor=SURFACE,
                                      gridspec_kw={"height_ratios": [3, 2]})
    for ax in (top, bottom):
        style(ax)
    ends = []  # (last p95, last second, label) for the direct labels
    for strategy, points in by_strategy.items():
        points = [p for p in points if p[0] < scenario.duration]
        seconds = [p[0] for p in points]
        color = STRATEGY_COLORS.get(strategy, INK_2)
        top.plot(seconds, [p[1] for p in points], color=color, linewidth=2, label=LABELS.get(strategy, strategy))
        values = [p[2] if lower == "errors" else p[3] for p in points]
        bottom.plot(seconds, values, color=color, linewidth=2)
        if points:
            ends.append((points[-1][1], seconds[-1], LABELS.get(strategy, strategy)))
    for event in scenario.events:
        what = f"{event.backend} slows to {event.delay_ms:.0f} ms" if event.action == "set_delay" \
            else f"{event.backend} killed"
        for ax in (top, bottom):
            ax.axvline(event.at, color=MUTED, linewidth=1, linestyle="--")
        top.annotate(what, (event.at, 1), xycoords=("data", "axes fraction"), xytext=(4, -12),
                     textcoords="offset points", fontsize=9, color=INK_2)
    p95s = [p[1] for points in by_strategy.values() for p in points if p[1]]
    if p95s and max(p95s) / min(p95s) > 10:
        # The strategies can differ by 10-100x; a log scale keeps both readable.
        top.set_yscale("log")
        top.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
        top.set_ylabel("p95 latency per second (ms, log scale)", color=INK_2, fontsize=10)
    else:
        top.set_ylim(0, max(p95s or [1]) * 1.15)
        top.set_ylabel("p95 latency per second (ms)", color=INK_2, fontsize=10)
    if lower == "errors":
        bottom.set_ylabel("Errors per second (per run)", color=INK_2, fontsize=10)
    else:
        backend = scenario.events[0].backend
        bottom.set_ylabel(f"Share of requests sent to {backend}", color=INK_2, fontsize=10)
        bottom.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
        bottom.set_ylim(0, 0.6)
    bottom.set_xlabel("Seconds since the load started", color=INK_2, fontsize=10)
    # Direct labels at the line ends, in ink (the line's colour carries identity).
    # Placed after the axis limits are final, and pushed apart when two lines end
    # within ~12 px of each other on screen.
    ends.sort()
    to_px = top.transData.transform
    for i, (value, second, label) in enumerate(ends):
        y = to_px((second, value))[1]
        close = any(abs(y - to_px((s2, v2))[1]) < 12 for j, (v2, s2, _) in enumerate(ends) if j != i)
        nudge = (-6 if i == 0 else 6) if close else 0
        top.annotate(label, (second, value), xytext=(4, nudge), textcoords="offset points",
                     va="center", fontsize=9, color=INK)
    top.legend(frameon=False, fontsize=9, labelcolor=INK, loc="upper left")
    fig.suptitle(f"{scenario_name.replace('_', ' ')}: latency over time (pooled over repeats)", color=INK, fontsize=12)
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return path


def plot_batch(batch: str | None = None, out_dir: Path = RESULTS_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(database_url()) as conn:
        batch = batch or latest_batch(conn)
        rows = conn.execute(BARS_SQL, (batch,)).fetchall()
        scenarios = [s for s in SCENARIOS if any(r[0] == s for r in rows)]
        strategies = [s for s in STRATEGY_COLORS if any(r[1] == s for r in rows)]
        paths = [plot_bars(rows, strategies, scenarios, out_dir / "baseline_bars.png")]
        for scenario, lower in (("sudden_slowdown", "share"), ("server_crash", "errors")):
            if scenario in scenarios:
                timeline = conn.execute(TIMELINE_SQL, {"batch": batch, "scenario": scenario,
                                                       "backend": SCENARIOS[scenario].events[0].backend}).fetchall()
                duration = conn.execute("SELECT (notes::jsonb ->> 'duration')::float8 FROM experiment_runs"
                                        " WHERE notes::jsonb ->> 'batch' = %s AND scenario = %s LIMIT 1",
                                        (batch, scenario)).fetchone()[0]
                paths.append(plot_timeline(timeline, scenario, lower, out_dir / f"timeline_{scenario}.png", duration))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch", help="experiment batch label (default: the latest)")
    args = parser.parse_args()
    for path in plot_batch(args.batch):
        print(f"saved {path}")


if __name__ == "__main__":
    main()
