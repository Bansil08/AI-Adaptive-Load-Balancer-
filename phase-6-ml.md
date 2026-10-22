Phase 5 is done. Now do Phase 6 only. Read PROJECT_SPEC.md for context. Explain each ML idea in simple terms before coding it.

Part A — Supervised latency model:
1. ml/export_data.py: export training data using the SQL query from Phase 4. Features: active connections, CPU %, memory %, recent average latency of the backend at the time of the request. Target: response time in ms.
2. ml/train.py: split by time (train on earlier data, test on later data, and explain why a random split would be misleading here). Train linear regression and a decision tree or random forest. Report MAE on the test set for both, plus a trivial baseline (always predict the backend's recent average), and save the best model to a file.
3. lb/strategies/ml_latency.py: load the model at startup, predict latency for each healthy backend, pick the lowest. Measure the per-request prediction overhead.

Part B — Bandit:
4. lb/strategies/bandit.py: epsilon-greedy over healthy backends. Reward = negative response time. Use a sliding window or exponential decay so old observations fade and it adapts when a backend changes. Make epsilon configurable. Explain exploration vs exploitation.

Part C — Safety:
5. Both strategies fall back to least connections if the model is missing, prediction throws an error, or it exceeds a small time budget. Log every fallback.

6. Run both ML strategies through all four scenarios using the Phase 5 experiment runner, and regenerate the comparison charts with all four strategies.

Tests: the bandit chooses the best arm most of the time on a fixed simulated reward, it shifts to a new best arm after a change, fallbacks trigger correctly, and the ML strategy only picks healthy backends.

Report results honestly: where ML wins, where it ties or loses, and why. Never adjust experiments to make ML look better.

Done when: all four strategies have results on all four scenarios. Update CONCEPTS.md (supervised regression, MAE, time-based split, overfitting, multi-armed bandit, exploration vs exploitation, model staleness, inference overhead, fallback). Commit, then stop and summarise with the full results table.
