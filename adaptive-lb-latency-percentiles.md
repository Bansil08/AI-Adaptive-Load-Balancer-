# adaptive-lb: Add p50 and p99 Latency

Goal: report p50 (median), p95 and p99 latency everywhere the dashboard currently shows only p95, without cluttering the page.

---

## 1. Compute the percentiles

- [x] In the code that aggregates latency samples (live 5 s buckets and per-run experiment results), compute **p50, p95 and p99** from the same samples.
  - Use one shared percentile function so all three are calculated the same way (e.g. `numpy.percentile` or nearest-rank, but be consistent).
- [x] Store p50 and p99 alongside p95 in the per-run experiment results.
- [x] For each scenario × strategy, compute **mean ± std and min–max over the runs** for p50 and p99, the same way p95 is done now.
- [x] Re-run the comparison batch (or recompute from saved raw samples, if they're stored) so existing results include the new fields.
- [x] Keep older batches loading without errors: if a batch has no p50/p99, show "—" instead of crashing.

**Done when:** the API / results files contain p50, p95 and p99 for both live buckets and experiment runs.

---

## 2. Results table

- [x] Replace the single "p95 latency (ms)" column with three columns: **p50**, **p95**, **p99**.
  - Each shows mean ± std; put min–max in a tooltip or a smaller second line so the table stays readable.
- [x] Keep the "Avg latency" column.
- [x] Format values the same way as now (ms below 1 s, seconds above).

**Done when:** every row shows p50 / p95 / p99 and the table still fits without horizontal scrolling on a laptop screen.

---

## 3. Live latency chart

- [x] Add a small segmented toggle **p50 | p95 | p99** above the "latency per backend" chart.
- [x] Default to p95 (current behaviour).
- [x] Update the chart title to match the selection, e.g. "p99 latency per backend, ms".
- [x] Remember the choice in the page (and optionally in `localStorage`, wrapped in try/catch).

**Done when:** switching the toggle redraws the live chart with the chosen percentile within one refresh.

---

## 4. Experiment bar charts

- [x] Add the same **p50 | p95 | p99** toggle above the "Experiment results" bar charts, controlling all four charts at once.
- [x] Update each chart title ("unequal servers: p99 latency (ms)", etc.).
- [x] Default to p95.

**Done when:** one toggle switches all four scenario charts between percentiles.

---

## 5. Headline result

- [x] If the headline banner (from the improvements plan) is built, let it use the selected percentile, or mention p99 alongside p95 where the gap is large, e.g. traffic spike.

---

## 6. Methodology note

- [x] Add one line to the methodology text under "Experiment results":
  > "Percentiles are computed per run from all completed requests; p99 rests on ~1% of samples (~27 per 30 s run at ~90 req/s), so it is noisier than p95."
- [x] Adjust the numbers to match your actual request counts.

---

## 7. README and resume

- [x] Add p50 and p99 columns to the README results table.
- [x] If p99 shows a bigger improvement than p95 in some scenario (likely traffic spike or sudden slowdown), consider using it in the resume bullet. Use only numbers from the latest batch.

---

## Interview points this adds

- Why p99 matters more than average for load balancers (tail latency, fan-out requests)
- What a large p50→p99 gap means (some requests hit the slow server or a queue)
- Why p99 is noisier with short runs, and how you'd fix it (longer runs, more repeats, HDR histograms)
