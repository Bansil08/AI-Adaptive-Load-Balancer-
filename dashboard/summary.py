"""The headline and the per-scenario "who wins" cards, computed from a batch.

Used by the dashboard (/api/experiments) and the static results page
(experiments/export_static.py), so both always say the same thing, and the
text follows whichever batch is selected instead of being hard-coded.

A winner is only declared when it is clear: the best strategy's p95 must beat
every other strategy by more than the run-to-run noise. Two cells count as
tied when their mean ± standard deviation intervals overlap (with 5 runs that
is a deliberately cautious rule: it never calls noise a win). Calling the
crash scenario's 38.8 vs 38.9 ms a "win" would be the kind of overclaim this
project avoids.
"""

LABELS = {"round_robin": "Round robin", "least_connections": "Least connections",
          "ml_latency": "ML latency", "bandit": "Bandit"}
SCENARIO_LABELS = {"unequal_servers": "unequal servers", "sudden_slowdown": "sudden slowdown",
                   "traffic_spike": "traffic spike", "server_crash": "server crash"}

# One line under each scenario: what happened and why. Checked against the
# measured batch (see README "What the numbers say").
SCENARIO_NOTES = {
    "unequal_servers": "20 / 60 / 150 ms backends. Routing on latency sends ~99% to the fast one, which has "
                       "capacity to spare; least connections still gives the 150 ms server ~15%, so its p95 "
                       "is a slow answer.",
    "sudden_slowdown": "One of three equal backends becomes 10× slower halfway. Round robin keeps feeding it and "
                       "its queue grows; every adaptive strategy steers away within about a second, so they "
                       "end up within noise of each other.",
    "traffic_spike": "Load jumps from 30 to 180 req/s. Round robin's third overloads the slow backend and a "
                     "backlog builds; the others never overload any backend.",
    "server_crash": "The backends are identical, so after b dies every strategy is choosing between two equal "
                    "servers and routing can't help. The ~0.2% errors are requests in flight on b when it "
                    "died or sent before the circuit breaker and health checks removed it (retries were off "
                    "in these runs).",
}


def _interval(cell: dict) -> tuple[float, float]:
    p95 = cell["p95_latency_ms"]
    return p95["mean"] - p95["sd"], p95["mean"] + p95["sd"]


def _ms(value: float) -> str:
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f} ms"


def _change(new: float, old: float) -> float | None:
    return None if not old else (new - old) / old


def scenario_cards(cells: list[dict]) -> list[dict]:
    """One card per scenario: the winner (or the tie), its p95 and the change vs round robin."""
    cards = []
    for scenario in dict.fromkeys(c["scenario"] for c in cells):  # keeps the cells' order
        group = sorted((c for c in cells if c["scenario"] == scenario), key=lambda c: c["p95_latency_ms"]["mean"])
        best = group[0]
        best_low, best_high = _interval(best)
        # Everyone whose mean ± SD overlaps the best's is not clearly worse.
        tied = [c for c in group if _interval(c)[0] <= best_high]
        rr = next((c for c in group if c["strategy"] == "round_robin"), None)
        p95 = best["p95_latency_ms"]["mean"]
        rr_p95 = rr["p95_latency_ms"]["mean"] if rr else None
        cards.append({
            "scenario": scenario,
            "label": SCENARIO_LABELS.get(scenario, scenario.replace("_", " ")),
            "clear_winner": len(tied) == 1,
            "winners": [c["strategy"] for c in tied],
            "p95_ms": p95,
            "round_robin_p95_ms": rr_p95,
            "change_vs_round_robin": _change(p95, rr_p95) if rr_p95 is not None else None,
            "note": SCENARIO_NOTES.get(scenario, ""),
        })
    return cards


def headline(cells: list[dict]) -> dict | None:
    """The scenario where a clear winner beats the strongest simple baseline by the most.

    "Strongest simple baseline" is least connections (or round robin if the
    batch has no least connections): beating round robin is easy, so the
    headline is chosen by the harder comparison and then also states the
    change vs round robin.
    """
    best = None
    for card in scenario_cards(cells):
        if not card["clear_winner"]:
            continue
        group = {c["strategy"]: c["p95_latency_ms"]["mean"] for c in cells if c["scenario"] == card["scenario"]}
        baseline = "least_connections" if "least_connections" in group else "round_robin"
        winner = card["winners"][0]
        if winner == baseline or baseline not in group:
            continue
        gain = _change(card["p95_ms"], group[baseline])
        if gain is not None and (best is None or gain < best["change_vs_baseline"]):
            best = {**card, "strategy": winner, "baseline": baseline, "baseline_p95_ms": group[baseline],
                    "change_vs_baseline": gain}
    if best is None:
        return None
    text = (f"{LABELS.get(best['strategy'], best['strategy'])}: p95 {_ms(best['p95_ms'])} vs "
            f"{_ms(best['round_robin_p95_ms'])} for round robin ({best['change_vs_round_robin']:+.0%})")
    if best["baseline"] != "round_robin":
        text += (f" and {_ms(best['baseline_p95_ms'])} for {LABELS[best['baseline']].lower()} "
                 f"({best['change_vs_baseline']:+.0%})")
    best["text"] = text.replace("-", "−") + f" on {best['label']}"
    return best
