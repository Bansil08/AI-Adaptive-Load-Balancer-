Phase 6 is done. Now do Phase 7, the final phase. Read PROJECT_SPEC.md for context.

1. Dashboard (dashboard/): a Flask app with Chart.js that refreshes every few seconds and shows requests per second per backend, p95 latency per backend, backend health and circuit breaker state, the active strategy, and the experiment results table with charts. Use the views from Phase 4.

2. Final experiments: run every strategy across every scenario at least 3 times each, report the mean and spread, and regenerate all charts.

3. README.md, written for a recruiter or interviewer:
   - One-paragraph summary and the headline result (real numbers only)
   - Architecture diagram (Mermaid) and how one request flows through the system
   - Features grouped by OS, CN, DBMS, system design and ML
   - How to run everything in a few commands
   - Results table and charts, with an honest analysis including where ML did not help
   - Design decisions and trade-offs (thread pool vs async, UDP heartbeats, single DB writer, why retries are limited to GET)
   - Limitations: the load balancer is a single point of failure, and how an active-passive standby could fix that
   - Future work

4. Clean-up: remove dead code, make sure all tests pass, add a Makefile or scripts for common commands, and make sure a fresh clone runs following only the README.

5. Finalise CONCEPTS.md and add a short "Interview questions this project answers" section with 20 likely questions and a 2–3 line answer for each, based on my actual code.

6. Write 3 resume bullet points based only on the measured results.

Done when: a fresh clone works by following the README, all tests pass, and the results are final. Commit, then summarise.
