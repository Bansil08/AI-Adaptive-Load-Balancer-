# Database benchmarks (Phase 4)

Measured 2026-10-22 02:14 UTC by `python -m experiments.benchmark_db`, in a throwaway database (`adaptive_lb_bench`). Real measured numbers; nothing estimated.

## Setup

- Client: Darwin 25.1.0 arm64, Python 3.14.3, psycopg 3.3.6
- Server: PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) in Docker Desktop (default settings: shared_buffers 128MB, synchronous_commit on), reached over localhost
- Data for the index runs: 1,200,000 requests, 2,073,603 server_metrics, 2,073,606 health_checks over 8 days, 3 servers
- Each query run 3× with `EXPLAIN (ANALYZE, BUFFERS)`; the table shows the median execution time

## 1. Single-row vs batched inserts

| Method | Rows | Rows/second (median) |
|---|---:|---:|
| one INSERT + COMMIT per row | 3,000 | 3,060 |
| one INSERT per row, one transaction | 20,000 | 5,245 |
| executemany, batch of 10 per transaction | 50,000 | 10,087 |
| executemany, batch of 50 per transaction | 50,000 | 25,514 |
| executemany, batch of 200 per transaction | 50,000 | 52,512 |
| executemany, batch of 1000 per transaction | 50,000 | 60,800 |
| DBWriter end to end (batch 200, 200 ms) | 100,000 | 47,209 |

## 2. Dashboard queries without and with the indexes

| Query | Without indexes (ms) | With indexes (ms) | Speed-up |
|---|---:|---:|---:|
| p95/avg latency per server per minute (15 min) | 19.51 | 0.74 | 26× |
| error rate per server (15 min) | 34.48 | 0.44 | 78× |
| request count per server (15 min) | 19.07 | 0.43 | 44× |
| training export, latest 100 requests | 10,822.36 | 0.27 | 40,083× |

Index build time: `requests_server_received_idx` 0.45 s, `server_metrics_server_recorded_idx` 0.62 s. Index size: `requests_server_received_idx` 36 MB, `server_metrics_server_recorded_idx` 62 MB.

With the index, the **whole** training export (1,188,000 requests, 1,187,991 matched to a heartbeat) took 2.79 s. Without it, it was not attempted: the 100-row sample above already shows each request costing a full scan of server_metrics.

### Plans

<details><summary>p95/avg latency per server per minute (15 min): without indexes</summary>

```
GroupAggregate  (cost=8568.61..23802.87 rows=1496 width=116) (actual time=18.698..19.492 rows=45 loops=1)
  Group Key: s.id, (((s.address || ':'::text) || (s.port)::text)), (date_trunc('minute'::text, r.received_at))
  Buffers: shared hit=290 read=13349 written=96
  ->  Incremental Sort  (cost=8568.61..23724.33 rows=1496 width=52) (actual time=18.682..19.252 rows=1511 loops=1)
        Sort Key: s.id, (((s.address || ':'::text) || (s.port)::text)), (date_trunc('minute'::text, r.received_at))
        Presorted Key: s.id
        Full-sort Groups: 3  Sort Method: quicksort  Average Memory: 29kB  Peak Memory: 29kB
        Pre-sorted Groups: 3  Sort Method: quicksort  Average Memory: 56kB  Peak Memory: 56kB
        Buffers: shared hit=290 read=13349 written=96
        ->  Nested Loop  (cost=1000.13..23638.53 rows=1496 width=52) (actual time=18.282..18.928 rows=1511 loops=1)
              Join Filter: (s.id = r.server_id)
              Rows Removed by Join Filter: 3070
              Buffers: shared hit=290 read=13349 written=96
              ->  Index Scan using servers_pkey on servers s  (cost=0.13..12.18 rows=3 width=18) (actual time=0.001..0.004 rows=3 loops=1)
                    Buffers: shared hit=2
              ->  Materialize  (cost=1000.00..23544.08 rows=1496 width=20) (actual time=6.092..6.185 rows=1527 loops=3)
                    Buffers: shared hit=288 read=13349 written=96
                    ->  Gather  (cost=1000.00..23536.60 rows=1496 width=20) (actual time=18.273..18.382 rows=1527 loops=1)
                          Workers Planned: 2
                          Workers Launched: 2
                          Buffers: shared hit=288 read=13349 written=96
                          ->  Parallel Seq Scan on requests r  (cost=0.00..22387.00 rows=623 width=20) (actual time=17.495..17.536 rows=509 loops=3)
                                Filter: (received_at >= (now() - '00:15:00'::interval))
                                Rows Removed by Filter: 399491
                                Buffers: shared hit=288 read=13349 written=96
Planning:
  Buffers: shared hit=4
Planning Time: 0.113 ms
Execution Time: 19.510 ms
```

</details>

<details><summary>p95/avg latency per server per minute (15 min): with indexes</summary>

```
GroupAggregate  (cost=1605.87..4882.95 rows=1415 width=116) (actual time=0.177..0.705 rows=45 loops=1)
  Group Key: s.id, (((s.address || ':'::text) || (s.port)::text)), (date_trunc('minute'::text, r.received_at))
  Buffers: shared hit=71
  ->  Incremental Sort  (cost=1605.87..4808.66 rows=1415 width=52) (actual time=0.174..0.508 rows=1448 loops=1)
        Sort Key: s.id, (((s.address || ':'::text) || (s.port)::text)), (date_trunc('minute'::text, r.received_at))
        Presorted Key: s.id
        Full-sort Groups: 3  Sort Method: quicksort  Average Memory: 29kB  Peak Memory: 29kB
        Pre-sorted Groups: 3  Sort Method: quicksort  Average Memory: 55kB  Peak Memory: 55kB
        Buffers: shared hit=71
        ->  Nested Loop  (cost=13.35..4728.08 rows=1415 width=52) (actual time=0.013..0.344 rows=1448 loops=1)
              Buffers: shared hit=71
              ->  Index Scan using servers_pkey on servers s  (cost=0.13..12.18 rows=3 width=18) (actual time=0.001..0.001 rows=3 loops=1)
                    Buffers: shared hit=2
              ->  Bitmap Heap Scan on requests r  (cost=13.22..1561.40 rows=467 width=20) (actual time=0.011..0.035 rows=483 loops=3)
                    Recheck Cond: ((s.id = server_id) AND (received_at >= (now() - '00:15:00'::interval)))
                    Heap Blocks: exact=54
                    Buffers: shared hit=69
                    ->  Bitmap Index Scan on requests_server_received_idx  (cost=0.00..13.10 rows=467 width=0) (actual time=0.010..0.010 rows=483 loops=3)
                          Index Cond: ((server_id = s.id) AND (received_at >= (now() - '00:15:00'::interval)))
                          Buffers: shared hit=15
Planning:
  Buffers: shared hit=4
Planning Time: 0.049 ms
Execution Time: 0.714 ms
```

</details>

<details><summary>error rate per server (15 min): without indexes</summary>

```
Append  (cost=8567.36..47126.70 rows=4 width=84) (actual time=18.756..34.431 rows=4 loops=1)
  Buffers: shared hit=1634 read=25642 written=192
  ->  GroupAggregate  (cost=8567.36..23739.39 rows=3 width=84) (actual time=18.756..19.047 rows=3 loops=1)
        Group Key: s.id, (((s.address || ':'::text) || (s.port)::text))
        Buffers: shared hit=770 read=12869 written=96
        ->  Incremental Sort  (cost=8567.36..23720.59 rows=1496 width=46) (actual time=18.544..18.944 rows=1511 loops=1)
              Sort Key: s.id, (((s.address || ':'::text) || (s.port)::text))
              Presorted Key: s.id
              Full-sort Groups: 3  Sort Method: quicksort  Average Memory: 29kB  Peak Memory: 29kB
              Pre-sorted Groups: 3  Sort Method: quicksort  Average Memory: 56kB  Peak Memory: 56kB
              Buffers: shared hit=770 read=12869 written=96
              ->  Nested Loop Left Join  (cost=1000.13..23634.79 rows=1496 width=46) (actual time=18.237..18.794 rows=1511 loops=1)
                    Join Filter: (r.server_id = s.id)
                    Rows Removed by Join Filter: 3070
                    Buffers: shared hit=770 read=12869 written=96
                    ->  Index Scan using servers_pkey on servers s  (cost=0.13..12.18 rows=3 width=18) (actual time=0.001..0.003 rows=3 loops=1)
                          Buffers: shared hit=2
                    ->  Materialize  (cost=1000.00..23544.08 rows=1496 width=14) (actual time=6.077..6.167 rows=1527 loops=3)
                          Buffers: shared hit=768 read=12869 written=96
                          ->  Gather  (cost=1000.00..23536.60 rows=1496 width=14) (actual time=18.231..18.335 rows=1527 loops=1)
                                Workers Planned: 2
                                Workers Launched: 2
                                Buffers: shared hit=768 read=12869 written=96
                                ->  Parallel Seq Scan on requests r  (cost=0.00..22387.00 rows=623 width=14) (actual time=17.482..17.520 rows=509 loops=3)
                                      Filter: (received_at >= (now() - '00:15:00'::interval))
                                      Rows Removed by Filter: 399491
                                      Buffers: shared hit=768 read=12869 written=96
  ->  Finalize Aggregate  (cost=23387.27..23387.29 rows=1 width=84) (actual time=14.631..15.382 rows=1 loops=1)
        Buffers: shared hit=864 read=12773 written=96
        ->  Gather  (cost=23387.05..23387.26 rows=2 width=16) (actual time=14.588..15.378 rows=3 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=864 read=12773 written=96
              ->  Partial Aggregate  (cost=22387.05..22387.06 rows=1 width=16) (actual time=13.781..13.781 rows=1 loops=3)
                    Buffers: shared hit=864 read=12773 written=96
                    ->  Parallel Seq Scan on requests  (cost=0.00..22387.00 rows=7 width=2) (actual time=13.763..13.778 rows=5 loops=3)
                          Filter: ((server_id IS NULL) AND (received_at >= (now() - '00:15:00'::interval)))
                          Rows Removed by Filter: 399995
                          Buffers: shared hit=864 read=12773 written=96
Planning:
  Buffers: shared hit=4
Planning Time: 0.110 ms
Execution Time: 34.451 ms
```

</details>

<details><summary>error rate per server (15 min): with indexes</summary>

```
Append  (cost=4742.23..4799.90 rows=4 width=84) (actual time=0.419..0.426 rows=4 loops=1)
  Buffers: shared hit=89
  ->  HashAggregate  (cost=4742.23..4742.32 rows=3 width=84) (actual time=0.419..0.419 rows=3 loops=1)
        Group Key: s.id, ((s.address || ':'::text) || (s.port)::text)
        Batches: 1  Memory Usage: 24kB
        Buffers: shared hit=71
        ->  Nested Loop Left Join  (cost=13.35..4724.54 rows=1415 width=46) (actual time=0.016..0.285 rows=1448 loops=1)
              Buffers: shared hit=71
              ->  Index Scan using servers_pkey on servers s  (cost=0.13..12.18 rows=3 width=18) (actual time=0.001..0.001 rows=3 loops=1)
                    Buffers: shared hit=2
              ->  Bitmap Heap Scan on requests r  (cost=13.22..1561.40 rows=467 width=14) (actual time=0.012..0.037 rows=483 loops=3)
                    Recheck Cond: ((server_id = s.id) AND (received_at >= (now() - '00:15:00'::interval)))
                    Heap Blocks: exact=54
                    Buffers: shared hit=69
                    ->  Bitmap Index Scan on requests_server_received_idx  (cost=0.00..13.10 rows=467 width=0) (actual time=0.011..0.011 rows=483 loops=3)
                          Index Cond: ((server_id = s.id) AND (received_at >= (now() - '00:15:00'::interval)))
                          Buffers: shared hit=15
  ->  Aggregate  (cost=57.54..57.56 rows=1 width=84) (actual time=0.006..0.006 rows=1 loops=1)
        Buffers: shared hit=18
        ->  Index Scan using requests_server_received_idx on requests  (cost=0.43..57.43 rows=14 width=2) (actual time=0.002..0.004 rows=15 loops=1)
              Index Cond: ((server_id IS NULL) AND (received_at >= (now() - '00:15:00'::interval)))
              Buffers: shared hit=18
Planning:
  Buffers: shared hit=4
Planning Time: 0.102 ms
Execution Time: 0.442 ms
```

</details>

<details><summary>request count per server (15 min): without indexes</summary>

```
GroupAggregate  (cost=8567.36..23735.61 rows=3 width=55) (actual time=18.762..19.158 rows=3 loops=1)
  Group Key: s.id, (((s.address || ':'::text) || (s.port)::text))
  Buffers: shared hit=1154 read=12485 written=96
  ->  Incremental Sort  (cost=8567.36..23720.59 rows=1496 width=48) (actual time=18.542..19.049 rows=1511 loops=1)
        Sort Key: s.id, (((s.address || ':'::text) || (s.port)::text))
        Presorted Key: s.id
        Full-sort Groups: 3  Sort Method: quicksort  Average Memory: 29kB  Peak Memory: 29kB
        Pre-sorted Groups: 3  Sort Method: quicksort  Average Memory: 56kB  Peak Memory: 56kB
        Buffers: shared hit=1154 read=12485 written=96
        ->  Nested Loop Left Join  (cost=1000.13..23634.79 rows=1496 width=48) (actual time=18.209..18.880 rows=1511 loops=1)
              Join Filter: (r.server_id = s.id)
              Rows Removed by Join Filter: 3070
              Buffers: shared hit=1154 read=12485 written=96
              ->  Index Scan using servers_pkey on servers s  (cost=0.13..12.18 rows=3 width=21) (actual time=0.001..0.003 rows=3 loops=1)
                    Buffers: shared hit=2
              ->  Materialize  (cost=1000.00..23544.08 rows=1496 width=13) (actual time=6.068..6.196 rows=1527 loops=3)
                    Buffers: shared hit=1152 read=12485 written=96
                    ->  Gather  (cost=1000.00..23536.60 rows=1496 width=13) (actual time=18.203..18.416 rows=1527 loops=1)
                          Workers Planned: 2
                          Workers Launched: 2
                          Buffers: shared hit=1152 read=12485 written=96
                          ->  Parallel Seq Scan on requests r  (cost=0.00..22387.00 rows=623 width=13) (actual time=17.412..17.457 rows=509 loops=3)
                                Filter: (received_at >= (now() - '00:15:00'::interval))
                                Rows Removed by Filter: 399491
                                Buffers: shared hit=1152 read=12485 written=96
Planning:
  Buffers: shared hit=6
Planning Time: 0.097 ms
Execution Time: 19.173 ms
```

</details>

<details><summary>request count per server (15 min): with indexes</summary>

```
HashAggregate  (cost=4738.69..4738.75 rows=3 width=55) (actual time=0.407..0.407 rows=3 loops=1)
  Group Key: s.id, ((s.address || ':'::text) || (s.port)::text)
  Batches: 1  Memory Usage: 24kB
  Buffers: shared hit=71
  ->  Nested Loop Left Join  (cost=13.35..4724.54 rows=1415 width=48) (actual time=0.012..0.271 rows=1448 loops=1)
        Buffers: shared hit=71
        ->  Index Scan using servers_pkey on servers s  (cost=0.13..12.18 rows=3 width=21) (actual time=0.001..0.001 rows=3 loops=1)
              Buffers: shared hit=2
        ->  Bitmap Heap Scan on requests r  (cost=13.22..1561.40 rows=467 width=13) (actual time=0.010..0.034 rows=483 loops=3)
              Recheck Cond: ((server_id = s.id) AND (received_at >= (now() - '00:15:00'::interval)))
              Heap Blocks: exact=54
              Buffers: shared hit=69
              ->  Bitmap Index Scan on requests_server_received_idx  (cost=0.00..13.10 rows=467 width=0) (actual time=0.010..0.010 rows=483 loops=3)
                    Index Cond: ((server_id = s.id) AND (received_at >= (now() - '00:15:00'::interval)))
                    Buffers: shared hit=15
Planning:
  Buffers: shared hit=6
Planning Time: 0.039 ms
Execution Time: 0.413 ms
```

</details>

<details><summary>training export, latest 100 requests: without indexes</summary>

```
Limit  (cost=51636.47..5215242.40 rows=100 width=100) (actual time=175.840..13776.755 rows=100 loops=1)
  Buffers: shared hit=16005 read=1922000
  ->  Nested Loop Left Join  (cost=51636.47..61312708393.43 rows=1187400 width=100) (actual time=136.852..13737.741 rows=100 loops=1)
        Buffers: shared hit=16005 read=1922000
        ->  Index Scan Backward using requests_pkey on requests r  (cost=0.43..44812.43 rows=1187400 width=48) (actual time=0.035..0.214 rows=100 loops=1)
              Filter: (server_id IS NOT NULL)
              Rows Removed by Filter: 2
              Buffers: shared hit=5
        ->  Limit  (cost=51636.04..51636.05 rows=1 width=28) (actual time=137.370..137.370 rows=1 loops=100)
              Buffers: shared hit=16000 read=1922000
              ->  Sort  (cost=51636.04..52212.04 rows=230400 width=28) (actual time=137.368..137.368 rows=1 loops=100)
                    Sort Key: server_metrics.recorded_at DESC
                    Sort Method: top-N heapsort  Memory: 25kB
                    Buffers: shared hit=16000 read=1922000
                    ->  Seq Scan on server_metrics  (cost=0.00..50484.04 rows=230400 width=28) (actual time=0.003..98.139 rows=691166 loops=100)
                          Filter: ((recorded_at <= r.received_at) AND (server_id = r.server_id))
                          Rows Removed by Filter: 1382437
                          Buffers: shared hit=16000 read=1922000
Planning Time: 0.196 ms
JIT:
  Functions: 11
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 0.687 ms, Inlining 6.333 ms, Optimization 17.706 ms, Emission 14.959 ms, Total 39.686 ms
Execution Time: 13777.920 ms
```

</details>

<details><summary>training export, latest 100 requests: with indexes</summary>

```
Limit  (cost=0.85..84.27 rows=100 width=100) (actual time=0.009..0.259 rows=100 loops=1)
  Buffers: shared hit=405
  ->  Nested Loop Left Join  (cost=0.85..990822.10 rows=1187880 width=100) (actual time=0.009..0.254 rows=100 loops=1)
        Buffers: shared hit=405
        ->  Index Scan Backward using requests_pkey on requests r  (cost=0.43..44812.43 rows=1187880 width=48) (actual time=0.003..0.010 rows=100 loops=1)
              Filter: (server_id IS NOT NULL)
              Rows Removed by Filter: 2
              Buffers: shared hit=5
        ->  Limit  (cost=0.43..0.78 rows=1 width=28) (actual time=0.002..0.002 rows=1 loops=100)
              Buffers: shared hit=400
              ->  Index Scan Backward using server_metrics_server_recorded_idx on server_metrics  (cost=0.43..80959.50 rows=230400 width=28) (actual time=0.002..0.002 rows=1 loops=100)
                    Index Cond: ((server_id = r.server_id) AND (recorded_at <= r.received_at))
                    Buffers: shared hit=400
Planning Time: 0.041 ms
Execution Time: 0.267 ms
```

</details>

## 3. Retention: batched vs one big DELETE

| How | Rows deleted | Transactions | Total time | Each transaction (rows locked this long): median / longest |
|---|---:|---:|---:|---:|
| Batches of 5000 (`scripts/retention.py`) | 150,104 | 31 | 1.35 s | 67.4 ms / 88.1 ms |
| One `DELETE` | 150,005 | 1 | 0.19 s | 190.9 ms |

Batching does not make the delete faster in total; it makes each transaction short, so rows are locked, and dead versions pile up, for milliseconds at a time instead of for the whole delete.

Other tables, same job (rows older than 7 days): health_checks 259,350 rows in 52 batches (0.53 s, transactions median 10.1 ms, longest 12.1 ms), server_metrics 259,371 rows in 52 batches (0.58 s, transactions median 10.4 ms, longest 19.7 ms).
