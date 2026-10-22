# adaptive-lb: Improvement Plan

Goal: make the project easy to understand in 30 seconds, easy to run in one command, and easy to show on a resume and in interviews. No full UI rewrite, no paid hosting.

Work through the sections in order. Each task has a "Done when" check.

---

## 1. Headline result on the dashboard

- [ ] Add a summary banner at the top of the dashboard (below the title row) that shows the key takeaway from the selected experiment batch.
  - Example: "ML latency routing: p95 31 ms vs 170 ms for round robin on unequal servers (−82%)"
  - Compute it from the batch data, not hardcoded, so it updates when a new batch is selected.
- [ ] Show the best strategy per scenario as small cards (scenario name, winner, p95, % improvement vs round robin).

**Done when:** someone opening the page sees which strategy wins and by how much, without reading any chart.

---

## 2. Dashboard UI fixes

- [ ] **Separate color palettes.**
  - The line charts color *backends* blue/orange/green, and the bar charts use the same colors for *strategies*. That's confusing.
  - Give strategies their own fixed palette, used consistently everywhere (bars, legend, strategy badge).
- [ ] *(Optional)* **Scale in skewed charts.** In "sudden slowdown" (2.33 s) and "traffic spike" (3.84 s), round robin's bar makes the other bars look like zero. The results table below already gives exact values, so this is only a nice-to-have.
  - Add a linear/log scale toggle on those two charts.
- [ ] **Explain the crash scenario.** Add a one-line note under "server crash" explaining why all strategies are roughly equal (the circuit breaker + retries handle the failure regardless of routing strategy). Confirm this is actually the reason before writing it.
- [ ] Add a **"Lower is better"** hint on each latency chart (it's currently only in the paragraph above).
- [ ] Check the layout at narrow widths (laptop at 1280px, and phone width) so the 4 result cards wrap instead of squashing.

**Done when:** colors mean one thing each and the crash scenario result is explained.

---

## 3. Live controls (the main UX upgrade)

Add a control panel so the adaptive behaviour can be demonstrated live.

- [ ] **Strategy switcher**: a dropdown (Round robin / Least conn / ML latency / Bandit) that changes the LB strategy at runtime.
  - Backend: an admin endpoint, e.g. `POST /admin/strategy {"strategy": "ml_latency"}`
- [ ] **Fault injection buttons** per backend:
  - "Slow down" (adds latency, e.g. +150 ms)
  - "Crash" (stops responding / returns errors)
  - "Restore"
  - Backend: admin endpoints on each backend, e.g. `POST /admin/fault {"type": "slow", "ms": 150}`
- [ ] **Load generator toggle**: start/stop background load at a chosen rate (e.g. 30 / 60 / 120 rps), so the charts move without running a separate script.
- [ ] Add an **event marker** on the live charts when a strategy is switched or a fault is injected (vertical line + label), so cause and effect are visible.
- [ ] Bind admin endpoints to localhost only, or protect them with a simple token, so they can't be abused if ever exposed.

**Done when:** you can slow down a backend from the UI and watch ML routing shift traffic away from it on the requests/s chart within a few seconds.

---

## 4. One-command setup

- [ ] Add `docker-compose.yml` that starts: load balancer, 3 backends (with different base latencies), dashboard, and optionally the load generator.
- [ ] Add a `Makefile` (or scripts) with:
  - `make up`: start everything
  - `make demo`: start everything + background load
  - `make experiment`: run the full comparison batch
  - `make down`
- [ ] Pin dependency versions; make sure a fresh clone works on another machine.
- [ ] Ship the trained ML model (or a script that trains it in under a minute) so the ML strategy works out of the box.

**Done when:** a fresh clone + `docker compose up` gives a working dashboard at `http://localhost:5050` with no manual steps.

---

## 5. Static results page (the deployed link)

- [ ] Add an export script that writes experiment batch results to `docs/data/*.json`.
- [ ] Build a static version of the "Experiment results" section (same charts, reading the JSON) in `docs/`.
- [ ] Include on the page:
  - The headline result
  - The 4 scenario charts with the fixes from section 2
  - A short "How it works" section (architecture diagram: client → LB → backends, plus where health checks, circuit breaker, and the ML model sit)
  - Methodology: fresh backends + LB per run, 30 s open-loop load, 80 runs, mean ± std, min–max
  - Links to the GitHub repo and demo video
- [ ] Publish with GitHub Pages (Settings → Pages → `docs/` folder).

**Done when:** there is a public URL that shows the results without anything running.

---

## 6. Demo video (60–90 s)

Record a screen capture following this script:

1. Run `docker compose up`, open the dashboard (5 s)
2. Round robin running under load; all backends healthy (10 s)
3. Click "Slow down" on 9002; its p95 rises and round robin keeps sending it traffic (15 s)
4. Switch to ML latency; traffic shifts away from 9002 and p95 recovers (20 s)
5. Click "Crash" on 9003; circuit breaker opens, retries absorb errors (15 s)
6. Scroll to experiment results and point at the headline numbers (10 s)

- [ ] Upload as YouTube (unlisted) or Loom.
- [ ] Also export a short GIF of steps 3–4 for the README.

---

## 7. README

Structure:

1. Title + one-line description
2. GIF of the live demo
3. Results table (scenario × strategy, p95 mean ± std) + headline numbers
4. Links: static results page, demo video
5. Quick start (`docker compose up`)
6. Architecture diagram + short explanation of each component
7. Routing strategies: how each one works (RR, least connections, ML latency, bandit)
8. Reliability features: health checks, circuit breaker, retries
9. Experiment methodology
10. Limitations and next steps (honest: single machine, synthetic backends, LB not horizontally scaled)

---

## 8. Resume entry

```
Adaptive Load Balancer | Python, [stack] · GitHub · Demo
- Built an HTTP load balancer with health checks, circuit breakers and retries,
  comparing round robin, least connections, ML latency prediction and a
  multi-armed bandit router
- ML-based routing cut p95 latency ~82% vs round robin on heterogeneous servers
  (31 ms vs 170 ms) and held p95 at 33 ms during a traffic spike where round
  robin reached 3.8 s
- Designed a reproducible benchmark (80 runs, 4 failure scenarios, open-loop
  load) with a live monitoring dashboard
```

Re-check every number against your latest batch before submitting.

---

## 9. Interview prep checklist

Be ready to answer:

- [ ] Why does ML beat least connections here? When would it not?
- [ ] Why are all strategies roughly equal in the crash scenario?
- [ ] What features does the ML model use? How do you avoid stale predictions?
- [ ] ML vs bandit: what is the exploration–exploitation trade-off?
- [ ] Why open-loop load instead of closed-loop? (Closed-loop hides tail latency.)
- [ ] What changes at 100 backends, or when the LB itself becomes the bottleneck?
- [ ] How does this compare to Nginx / Envoy (least-request, outlier detection)?
- [ ] What is the LB's own overhead per request?

Before every interview: run `make demo` once to confirm it works, and keep the video link ready as a backup.

---

## Suggested order

| Priority | Section | Why |
|---|---|---|
| 1 | 2. UI fixes | Quick, removes visible rough edges |
| 2 | 1. Headline result | Biggest clarity gain for little work |
| 3 | 4. One-command setup | Needed for the demo and for reviewers |
| 4 | 3. Live controls | The real "wow" moment in interviews |
| 5 | 6. Demo video | Backup + resume link |
| 6 | 5. Static results page | Public link for the resume |
| 7 | 7. README + 8. Resume | Final packaging |
