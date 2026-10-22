
Phase 4 is done. Now do Phase 5 only. Read PROJECT_SPEC.md for context.

1. Least connections strategy (lb/strategies/least_connections.py) using the live stats.

2. Load generator (experiments/load_generator.py): multithreaded, sends requests at a target rate (requests per second) for a set duration, and records each request's latency and status locally as well.

3. Scenarios (experiments/scenarios.py), each fully automated and repeatable:
   a. Unequal servers: backends with clearly different speeds (use Docker CPU limits and/or delays).
   b. Sudden slowdown: one backend's delay jumps sharply halfway through the run (add a control endpoint on the backend to change its delay at runtime).
   c. Traffic spike: the request rate jumps from low to high partway through.
   d. Crash: one backend is killed partway through.

4. Experiment runner (experiments/run_experiments.py): for each strategy and scenario, create an experiment_runs row, tag all requests with its run_id, run the scenario several times, and compute avg latency, p95 latency, error rate and throughput into run_results. Make the whole run reproducible with one command and a fixed random seed.

5. Plots (experiments/plot_results.py): bar charts comparing strategies per scenario, and latency-over-time charts for the slowdown and crash scenarios. Save as PNG files.

Run round robin and least connections across all four scenarios now. Report the measured results honestly and briefly explain why each strategy behaved the way it did.

Done when: one command runs all baseline experiments and produces the results table and charts. Update CONCEPTS.md (least connections, p95 vs average, load testing, why baselines matter). Commit, then stop and summarise with the results table.
