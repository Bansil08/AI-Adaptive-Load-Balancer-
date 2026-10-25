"""Dashboard: headline / winner cards, the control endpoints' safety checks, and the demo load.

The control endpoints are tested against small fake HTTP servers standing in
for the LB's admin API and a backend, so no database or LB is needed.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import dashboard.app as dashboard
from dashboard.load import DemoLoad
from dashboard.summary import headline, scenario_cards, summaries


def cell(scenario, strategy, mean, sd=1.0, p99=None):
    spread = {"mean": mean, "sd": sd, "min": mean - sd, "max": mean + sd}
    p99_spread = None if p99 is None else {"mean": p99, "sd": sd, "min": p99 - sd, "max": p99 + sd}
    return {"scenario": scenario, "strategy": strategy, "runs": 5, "p95_latency_ms": spread,
            "p50_latency_ms": None, "p99_latency_ms": p99_spread,
            "avg_latency_ms": spread, "error_rate": {"mean": 0, "sd": 0, "min": 0, "max": 0},
            "throughput_rps": {"mean": 90, "sd": 0, "min": 90, "max": 90}}


CELLS = [
    cell("unequal_servers", "round_robin", 170), cell("unequal_servers", "least_connections", 166),
    cell("unequal_servers", "ml_latency", 31), cell("unequal_servers", "bandit", 70),
    cell("traffic_spike", "round_robin", 3840), cell("traffic_spike", "least_connections", 164),
    cell("traffic_spike", "ml_latency", 33), cell("traffic_spike", "bandit", 68),
    # within noise of each other: no winner may be declared
    cell("server_crash", "round_robin", 38.9, 0.2), cell("server_crash", "least_connections", 38.8, 0.1),
    cell("server_crash", "ml_latency", 39.6, 0.3), cell("server_crash", "bandit", 38.9, 0.2),
]


# --- headline + cards ---------------------------------------------------------------------

def test_cards_name_a_winner_only_when_it_beats_the_noise():
    cards = {c["scenario"]: c for c in scenario_cards(CELLS)}
    assert cards["unequal_servers"]["clear_winner"] and cards["unequal_servers"]["winners"] == ["ml_latency"]
    assert cards["unequal_servers"]["change_vs_round_robin"] == pytest.approx(31 / 170 - 1)
    crash = cards["server_crash"]
    assert not crash["clear_winner"]
    # ML's 39.6 ± 0.3 doesn't overlap the best (38.8 ± 0.1): it's clearly worse, not tied.
    assert set(crash["winners"]) == {"least_connections", "round_robin", "bandit"}
    assert "identical" in crash["note"] and "retries were off" in crash["note"]


def test_headline_is_chosen_against_the_strongest_simple_baseline():
    # The spike beats round robin by more (−99%), but the unequal-servers win is
    # bigger against least connections (−81% vs −80%), so that is the headline.
    h = headline(CELLS)
    assert h["scenario"] == "unequal_servers" and h["strategy"] == "ml_latency"
    assert h["text"] == ("ML latency: p95 31 ms vs 170 ms for round robin (−82%) and 166 ms for least "
                         "connections (−81%) on unequal servers")


def test_no_headline_when_nothing_clearly_wins():
    assert headline([c for c in CELLS if c["scenario"] == "server_crash"]) is None


def test_headline_and_cards_follow_the_chosen_percentile():
    # As measured on sudden slowdown: tied at p95, but at p99 only ML avoids the slowed backend.
    cells = [cell("sudden_slowdown", "round_robin", 2333, 175, p99=3121),
             cell("sudden_slowdown", "least_connections", 43.6, 2.5, p99=327.6),
             cell("sudden_slowdown", "ml_latency", 41.9, 2.1, p99=46.7)]
    assert not scenario_cards(cells)[0]["clear_winner"]
    [card] = scenario_cards(cells, "p99")
    assert card["clear_winner"] and card["winners"] == ["ml_latency"] and card["value_ms"] == 46.7
    assert headline(cells, "p99")["text"] == ("ML latency: p99 47 ms vs 3.12 s for round robin (−99%) and 328 ms "
                                              "for least connections (−86%) on sudden slowdown")
    # p50 was never measured in these cells: no card, no headline, no crash.
    assert summaries(cells)["p50"] == {"headline": None, "cards": []}


def test_headline_formats_seconds():
    cells = [cell("traffic_spike", "round_robin", 3840), cell("traffic_spike", "ml_latency", 33)]
    assert "3.84 s for round robin" in headline(cells)["text"]


# --- fake admin API + backend -------------------------------------------------------------------

class Fake:
    """A tiny HTTP server recording what it received and answering with JSON."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.seen.append((self.command, self.path, dict(self.headers), body))
                status, payload = fake.routes.get((self.command, self.path), (404, {"error": "no route"}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _reply

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.address = f"127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fakes():
    backend = Fake({("POST", "/fault"): (200, {"fault": "slow"}), ("GET", "/fault"): (200, {"fault": "none"})})
    admin = Fake({("GET", "/backends"): (200, {"backends": [{"address": backend.address}]}),
                  ("POST", "/strategy"): (200, {"strategy": "bandit", "previous": "round_robin"})})
    dashboard.app.config.update(ADMIN_URL=f"http://{admin.address}", ADMIN_TOKEN="tok")
    yield admin, backend
    admin.close()
    backend.close()


@pytest.fixture
def client():
    return dashboard.app.test_client()


def test_strategy_switch_is_forwarded_with_the_token(fakes, client):
    admin, _ = fakes
    resp = client.post("/api/control/strategy", json={"strategy": "bandit"})
    assert resp.status_code == 200 and resp.get_json()["strategy"] == "bandit"
    method, path, headers, body = admin.seen[-1]
    assert (method, path, json.loads(body)) == ("POST", "/strategy", {"strategy": "bandit"})
    assert headers["X-Admin-Token"] == "tok"
    assert client.get("/api/events").get_json()[-1]["label"] == "strategy → bandit"


def test_fault_goes_only_to_the_lbs_own_backends(fakes, client):
    admin, backend = fakes
    resp = client.post("/api/control/fault", json={"backend": backend.address, "type": "slow", "ms": 150})
    assert resp.status_code == 200
    assert json.loads(backend.seen[-1][3]) == {"type": "slow", "ms": 150.0}
    assert backend.seen[-1][2]["X-Admin-Token"] == "tok"
    # Any other address is refused before a request is sent (no SSRF through the dashboard).
    sent_before = len(backend.seen)
    resp = client.post("/api/control/fault", json={"backend": "169.254.169.254:80", "type": "crash"})
    assert resp.status_code == 400 and "not one of the LB's backends" in resp.get_json()["error"]
    assert len(backend.seen) == sent_before


def test_faults_lists_each_backends_state(fakes, client):
    _, backend = fakes
    assert client.get("/api/control/faults").get_json() == {backend.address: "none"}


def test_controls_refuse_other_origins_and_non_json(fakes, client):
    admin, _ = fakes
    seen = len(admin.seen)
    resp = client.post("/api/control/strategy", json={"strategy": "bandit"},
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    resp = client.post("/api/control/strategy", data="strategy=bandit",
                       content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 400
    assert len(admin.seen) == seen   # nothing was forwarded
    # The page's own origin is fine.
    resp = client.post("/api/control/strategy", json={"strategy": "bandit"}, headers={"Origin": "http://localhost"})
    assert resp.status_code == 200


def test_admin_errors_are_passed_on_not_swallowed(fakes, client):
    admin, _ = fakes
    admin.routes[("POST", "/strategy")] = (403, {"error": "missing or wrong X-Admin-Token"})
    resp = client.post("/api/control/strategy", json={"strategy": "bandit"})
    assert resp.status_code == 403 and "X-Admin-Token" in resp.get_json()["error"]


def test_lb_unreachable_is_a_502(client):
    dashboard.app.config["ADMIN_URL"] = "http://127.0.0.1:1"
    resp = client.post("/api/control/strategy", json={"strategy": "bandit"})
    assert resp.status_code == 502


# --- demo load ----------------------------------------------------------------------------------

def test_demo_load_sends_at_roughly_the_rate_and_stops():
    target = Fake({("GET", "/"): (200, {})})
    load = DemoLoad(f"http://{target.address}")
    try:
        load.start(50)
        time.sleep(1.0)
        load.start(50)                 # starting again only changes the rate: one generator
        time.sleep(1.0)
        load.stop()
        time.sleep(0.6)                # workers finish their current request
        status = load.status()
        assert not status["running"] and status["errors"] == 0
        assert 50 <= status["sent"] <= 160          # ~100 expected over 2 s (Poisson, so loose bounds)
        sent = len(target.seen)
        time.sleep(0.5)
        assert len(target.seen) == sent             # really stopped
    finally:
        load.stop()
        target.close()


@pytest.mark.parametrize("rate", [0, -1, 10_000])
def test_demo_load_rejects_bad_rates(rate):
    with pytest.raises(ValueError):
        DemoLoad("http://127.0.0.1:1").start(rate)


def test_load_endpoint_start_and_stop(client, monkeypatch):
    calls = []

    class Recorder:
        def start(self, rate):
            calls.append(("start", rate))

        def stop(self):
            calls.append(("stop",))

        def status(self):
            return {"running": bool(calls) and calls[-1][0] == "start"}

    monkeypatch.setitem(dashboard.app.config, "LOAD", Recorder())
    assert client.post("/api/control/load", json={"rate": 60}).get_json() == {"running": True}
    assert client.post("/api/control/load", json={"rate": 0}).get_json() == {"running": False}
    assert client.post("/api/control/load", json={"rate": "fast"}).status_code == 400
    assert calls == [("start", 60.0), ("stop",)]


def test_controls_refuse_unknown_host_names_dns_rebinding(fakes, client):
    # evil.example re-pointed at 127.0.0.1 is "same-origin" with itself: the Host check stops it.
    resp = client.post("/api/control/strategy", json={"strategy": "bandit"},
                       headers={"Host": "evil.example:5050", "Origin": "http://evil.example:5050"})
    assert resp.status_code == 403 and "DASHBOARD_HOSTS" in resp.get_json()["error"]


def test_stop_discards_unsent_jobs_so_a_restart_does_not_burst():
    load = DemoLoad("http://127.0.0.1:1")
    for _ in range(50):
        load._jobs.put_nowait(None)      # as if the workers had fallen behind
    load.stop()
    assert load._jobs.qsize() == 0
