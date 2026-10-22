# Failover demo: kill a backend under traffic, then restart it

Measured 2026-10-22 01:15 UTC by `python -m experiments.demo_failover`. Real measured numbers.

Setup: 3 backends (10 ms delay) with heartbeat agents; 6 client threads sending GET in a loop (20 ms pause); backend b is SIGKILLed at t = 5 s and restarted at t = 12 s. Health checks every 1 s (timeout 0.5 s, down after 2 failures, up after 2 passes); circuit breaker opens after 3 consecutive failures, 3 s cooldown, 2 half-open trials. The 'b' columns are sampled from the admin API at the end of each second.

## Run: retries off

| t (s) | served by a | served by b | served by c | errors | b healthy | b circuit | b heartbeat age (s) |
|------:|------------:|------------:|------------:|-------:|-----------|-----------|--------------------:|
| 0–1 | 54 | 54 | 54 | 0 | True | closed | 0.1 |
| 1–2 | 53 | 52 | 52 | 0 | True | closed | 0.1 |
| 2–3 | 53 | 54 | 54 | 0 | True | closed | 0.1 |
| 3–4 | 54 | 54 | 54 | 0 | True | closed | 0.1 |
| 4–5 | 52 | 52 | 52 | 0 | True | closed | 0.1 |
| 5–6 ← SIGKILL backend b | 79 | 0 | 78 | 3 | True | open | 1.0 |
| 6–7 | 78 | 0 | 78 | 0 | False | open | 2.0 |
| 7–8 | 79 | 0 | 78 | 0 | False | open | 3.0 |
| 8–9 | 78 | 0 | 79 | 0 | False | open | 4.0 |
| 9–10 | 80 | 0 | 79 | 0 | False | open | 5.0 |
| 10–11 | 78 | 0 | 79 | 0 | False | open | 6.0 |
| 11–12 | 80 | 0 | 79 | 0 | False | open | 7.2 |
| 12–13 ← restarted backend b | 79 | 0 | 79 | 0 | False | open | 8.0 |
| 13–14 | 77 | 5 | 77 | 0 | True | closed | 0.9 |
| 14–15 | 54 | 53 | 53 | 0 | True | closed | 0.9 |
| 15–16 | 52 | 53 | 52 | 0 | True | closed | 0.9 |
| 16–17 | 52 | 52 | 53 | 0 | True | closed | 0.9 |
| 17–18 | 50 | 50 | 50 | 0 | True | closed | 0.9 |
| 18–19 | 51 | 51 | 50 | 0 | True | closed | 0.9 |
| 19–20 | 53 | 53 | 54 | 0 | True | closed | 0.9 |

- 3 failed requests out of 3156; the last error was in second 5–6, i.e. errors stopped within 1 s of the kill.
- b served traffic again from second 13 (1 s after the restart).
- LB log: `backend 127.0.0.1:50741 is DOWN (2 consecutive failed checks; last: tcp: [Errno 61] Connection refused)`; `backend 127.0.0.1:50741 is UP (2 consecutive passed checks)`

## Run: retries on (max_retries = 2)

| t (s) | served by a | served by b | served by c | errors | b healthy | b circuit | b heartbeat age (s) |
|------:|------------:|------------:|------------:|-------:|-----------|-----------|--------------------:|
| 0–1 | 55 | 54 | 54 | 0 | True | closed | 0.1 |
| 1–2 | 52 | 53 | 52 | 0 | True | closed | 0.1 |
| 2–3 | 54 | 54 | 54 | 0 | True | closed | 0.0 |
| 3–4 | 53 | 52 | 53 | 0 | True | closed | 0.0 |
| 4–5 | 54 | 51 | 54 | 0 | True | closed | 0.0 |
| 5–6 ← SIGKILL backend b | 75 | 0 | 77 | 0 | True | open | 1.0 |
| 6–7 | 80 | 0 | 79 | 0 | False | open | 2.0 |
| 7–8 | 79 | 0 | 79 | 0 | False | open | 3.0 |
| 8–9 | 80 | 0 | 81 | 0 | False | open | 4.0 |
| 9–10 | 79 | 0 | 78 | 0 | False | open | 5.0 |
| 10–11 | 80 | 0 | 80 | 0 | False | open | 6.0 |
| 11–12 | 79 | 0 | 79 | 0 | False | open | 7.2 |
| 12–13 ← restarted backend b | 78 | 0 | 79 | 0 | False | open | 8.0 |
| 13–14 | 79 | 4 | 77 | 0 | True | closed | 0.9 |
| 14–15 | 52 | 53 | 53 | 0 | True | closed | 0.9 |
| 15–16 | 53 | 53 | 53 | 0 | True | closed | 0.9 |
| 16–17 | 52 | 52 | 51 | 0 | True | closed | 0.9 |
| 17–18 | 53 | 52 | 53 | 0 | True | closed | 0.9 |
| 18–19 | 52 | 53 | 53 | 0 | True | closed | 0.9 |
| 19–20 | 53 | 53 | 52 | 0 | True | closed | 0.9 |

- **No client saw an error.** 3167 requests succeeded.
- b served traffic again from second 13 (1 s after the restart).
- LB log: `backend 127.0.0.1:54594 is DOWN (2 consecutive failed checks; last: tcp: [Errno 61] Connection refused)`; `backend 127.0.0.1:54594 is UP (2 consecutive passed checks)`
