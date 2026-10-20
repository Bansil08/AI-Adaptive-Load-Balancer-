"""Tests for the benchmark's statistics helpers."""

from experiments.benchmark_concurrency import percentile


def test_nearest_rank_percentile():
    values = list(range(1, 21))  # 1..20
    assert percentile(values, 95) == 19  # ceil(0.95 * 20) = 19th value
    assert percentile(values, 100) == 20
    assert percentile(values, 50) == 10
    assert percentile([7.0], 95) == 7.0
