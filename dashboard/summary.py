"""The headline and the per-scenario "who wins" cards, computed from a batch.

Used by the dashboard (/api/experiments) and the static results page
(experiments/export_static.py), so both always say the same thing, and the
text follows whichever batch is selected instead of being hard-coded.

Both can be computed for p50, p95 (the default) or p99, so the page's
percentile toggle changes the cards and the headline too (summaries()).

A winner is only declared when it is clear: the best strategy's percentile must beat
every other strategy by more than the run-to-run noise. Two cells count as
tied when their mean ± standard deviation intervals overlap (with 5 runs that
is a deliberately cautious rule: it never calls noise a win), and a cell of a
single run, which can't measure noise, never wins. Calling the
crash scenario's 38.8 vs 38.9 ms a "win" would be the kind of overclaim this
project avoids.
"""

PERCENTILES = ("p50", "p95", "p99")
LABELS = {"round_robin": "Round robin", "least_connections": "Least connections",
          "ml_latency": "ML latency", "bandit": "Bandit"}
SCENARIO_LABELS = {"unequal_servers": "unequal servers", "sudden_slowdown": "sudden slowdown",
                   "traffic_spike": "traffic spike", "server_crash": "server crash"}

# One line under each scenario: what happened and why. Checked against the
# measured batch (see README "What the numbers say").
SCENARIO_NOTES = {
    "unequal_servers": "20 / 60 / 150 ms backends. Routing on latency sends ~99% to the fast one, which has "
                       "capacity to spare; least connections still gives the 150 ms server ~15%, so its p95 "
                       "is a slow answer. The bandit's ~4% there is under 5% but over 1%: a fine p95, a slow p99.",
    "sudden_slowdown": "One of three equal backends becomes 10× slower halfway. Round robin keeps feeding it and "
                       "its queue grows; every adaptive strategy steers away within about a second, so at p95 "
                       "they are within noise. p99 splits them: least connections still sends the slow backend "
                       "~9% of later traffic and the bandit ~3% (over 1% of all requests), ML latency 0.4%.",
    "traffic_spike": "Load jumps from 30 to 180 req/s. Round robin's third overloads the slow backend and a "
                     "backlog builds; the others never overload any backend.",
    "server_crash": "The backends are identical, so after b dies every strategy is choosing between two equal "
                    "servers and routing can't help. The ~0.2% errors are requests in flight on b when it "
                    "died or sent before the circuit breaker and health checks removed it (retries were off "
                    "in these runs).",
}


def _interval(cell: dict, key: str) -> tuple[float, float]:
    value = cell[key]
    return value["mean"] - value["sd"], value["mean"] + value["sd"]


def _ms(value: float) -> str:
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f} ms"


def _change(new: float, old: float) -> float | None:
    return None if not old else (new - old) / old


def scenario_cards(cells: list[dict], percentile: str = "p95") -> list[dict]:
    """One card per scenario: the winner (or the tie), its percentile and the change vs round robin.

    Cells without that percentile (an older batch has no p50 / p99) are left
    out; a scenario with none left gets no card.
    """
    key = f"{percentile}_latency_ms"
    cards = []
    for scenario in dict.fromkeys(c["scenario"] for c in cells):  # keeps the cells' order
        group = sorted((c for c in cells if c["scenario"] == scenario and c.get(key)),
                       key=lambda c: c[key]["mean"])
        if not group:
            continue
        best = group[0]
        best_high = _interval(best, key)[1]
        # Everyone whose mean ± SD overlaps the best's is not clearly worse. A cell
        # of one run has SD 0, which says nothing about noise: then nobody wins.
        if all(c.get("runs", 0) >= 2 for c in group):
            tied = [c for c in group if _interval(c, key)[0] <= best_high]
        else:
            tied = group
        rr = next((c for c in group if c["strategy"] == "round_robin"), None)
        value = best[key]["mean"]
        rr_value = rr[key]["mean"] if rr else None
        cards.append({
            "scenario": scenario,
            "label": SCENARIO_LABELS.get(scenario, scenario.replace("_", " ")),
            "percentile": percentile,
            "clear_winner": len(tied) == 1,
            "winners": [c["strategy"] for c in tied],
            "value_ms": value,
            "round_robin_ms": rr_value,
            "change_vs_round_robin": _change(value, rr_value) if rr_value is not None else None,
            "note": SCENARIO_NOTES.get(scenario, ""),
        })
    return cards


def headline(cells: list[dict], percentile: str = "p95") -> dict | None:
    """The scenario where a clear winner beats the strongest simple baseline by the most.

    "Strongest simple baseline" is least connections (or round robin if the
    batch has no least connections): beating round robin is easy, so the
    headline is chosen by the harder comparison and then also states the
    change vs round robin.
    """
    key = f"{percentile}_latency_ms"
    best = None
    for card in scenario_cards(cells, percentile):
        if not card["clear_winner"]:
            continue
        group = {c["strategy"]: c[key]["mean"] for c in cells if c["scenario"] == card["scenario"] and c.get(key)}
        baseline = "least_connections" if "least_connections" in group else "round_robin"
        winner = card["winners"][0]
        if winner == baseline or baseline not in group:
            continue
        gain = _change(card["value_ms"], group[baseline])
        if gain is not None and (best is None or gain < best["change_vs_baseline"]):
            best = {**card, "strategy": winner, "baseline": baseline, "baseline_ms": group[baseline],
                    "change_vs_baseline": gain}
    if best is None:
        return None
    # A batch run without round robin (--strategies) has nothing to compare with there.
    against = []
    if best["round_robin_ms"] is not None:
        against.append(f"{_ms(best['round_robin_ms'])} for round robin ({best['change_vs_round_robin']:+.0%})")
    if best["baseline"] != "round_robin":
        against.append(f"{_ms(best['baseline_ms'])} for {LABELS[best['baseline']].lower()} "
                       f"({best['change_vs_baseline']:+.0%})")
    text = (f"{LABELS.get(best['strategy'], best['strategy'])}: {percentile} {_ms(best['value_ms'])} vs "
            + " and ".join(against))
    best["text"] = text.replace("-", "−") + f" on {best['label']}"
    return best


def summaries(cells: list[dict]) -> dict:
    """Headline and cards for each percentile, for the page's p50 | p95 | p99 toggle."""
    return {p: {"headline": headline(cells, p), "cards": scenario_cards(cells, p)} for p in PERCENTILES}
