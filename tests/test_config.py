"""Tests for lb.config (no database needed)."""

import pytest
from psycopg.conninfo import conninfo_to_dict

from lb.config import DEFAULT_LB_CONFIG, ConcurrencySettings, ProxySettings, database_url, load_lb_config
from lb.strategies.base import Backend


def test_password_with_special_characters_is_not_misparsed(monkeypatch):
    # Regression: a URL-style string split 'p@ss:w/rd#1' into host 'ss', port 'w'.
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd#1 x'y")
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    monkeypatch.setenv("POSTGRES_PORT", "6543")

    parsed = conninfo_to_dict(database_url())

    assert parsed["password"] == "p@ss:w/rd#1 x'y"
    assert parsed["host"] == "db.example"
    assert parsed["port"] == "6543"


# --- load balancer config file ------------------------------------------------


def test_repo_config_file_loads_three_backends():
    cfg = load_lb_config(DEFAULT_LB_CONFIG)
    assert cfg.strategy == "round_robin"
    assert cfg.port == 8080
    # Must match the published ports of the backends in docker-compose.yml.
    assert [b.port for b in cfg.backends] == [9001, 9002, 9003]


def test_config_values_are_read(tmp_path):
    path = tmp_path / "lb.toml"
    path.write_text(
        'strategy = "round_robin"\n'
        'backends = ["10.0.0.1:80", "[::1]:81"]\n'
        "[listen]\nhost = \"0.0.0.0\"\nport = 9999\n"
        "[timeouts]\nclient = 1.5\nbackend_connect = 0.5\nbackend_read = 3\n"
        "[concurrency]\nworkers = 4\nqueue_size = 16\nshutdown_timeout = 2.5\n"
    )
    cfg = load_lb_config(path)
    assert cfg.concurrency == ConcurrencySettings(workers=4, queue_size=16, shutdown_timeout=2.5)
    assert (cfg.host, cfg.port) == ("0.0.0.0", 9999)
    assert cfg.backends == (Backend("10.0.0.1", 80), Backend("::1", 81))
    assert cfg.proxy == ProxySettings(client_timeout=1.5, backend_connect_timeout=0.5, backend_read_timeout=3.0)


def test_missing_sections_use_defaults(tmp_path):
    path = tmp_path / "lb.toml"
    path.write_text("")
    cfg = load_lb_config(path)
    assert (cfg.host, cfg.port, cfg.strategy, cfg.backends) == ("127.0.0.1", 8080, "round_robin", ())
    assert cfg.proxy == ProxySettings()
    assert cfg.concurrency == ConcurrencySettings()


@pytest.mark.parametrize(
    "content",
    ['backends = ["no-port"]', "[timeouts]\nbackend_read = 0", '[listen]\nport = "abc"', "backends = [5]", "backends = \"127.0.0.1:9001\"",
     "[concurrency]\nworkers = 0", "[concurrency]\nqueue_size = 1.5", '[concurrency]\nworkers = "4"',
     "[concurrency]\nworkers = true", "[concurrency]\nshutdown_timeout = -1",
     # TOML accepts inf/nan; join(inf) raised OverflowError mid-shutdown.
     "[concurrency]\nshutdown_timeout = inf", "[concurrency]\nshutdown_timeout = nan",
     "[timeouts]\nclient = inf",
     # A scalar where a table belongs used to escape as AttributeError.
     "concurrency = 32", "listen = 8080", 'timeouts = "fast"'],
)
def test_bad_config_values_raise_value_error(tmp_path, content):
    path = tmp_path / "lb.toml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_lb_config(path)
