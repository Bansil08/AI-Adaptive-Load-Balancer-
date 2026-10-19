Read PROJECT_SPEC.md again. Phase 0 is done. Now do Phase 1 only: the basic proxy.

Part A — Test backends (backend/app.py):
- A simple HTTP server using the standard library.
- Command-line options: --port, --delay-ms (artificial delay per request), --name.
- Endpoints: GET / returns JSON with the backend name and the delay used; GET /health returns 200 OK.
- Add a docker-compose service or a script that starts 3 backends on different ports with different delays (e.g. 20 ms, 60 ms, 150 ms).

Part B — HTTP parser (lb/http_parser.py):
- Parse an HTTP/1.1 request by hand from bytes read off a socket: request line (method, path, version), headers (case-insensitive), and body using Content-Length.
- Handle the request arriving in several recv() chunks, and reject malformed requests with 400.
- Parse responses from backends the same way.
- Before writing it, explain how TCP delivers a byte stream (not messages) and why that means we must read until the end of the headers and then exactly Content-Length bytes.

Part C — Proxy (lb/server.py, lb/proxy.py, lb/router.py, lb/strategies/base.py, lb/strategies/round_robin.py):
- Listen on a configurable port. For now, handle one connection per thread (thread-per-connection); we replace this with a thread pool in Phase 2.
- Define a LoadBalancingStrategy abstract base class with a choose(backends, request) method; implement RoundRobin.
- Forward the request to the chosen backend over a new TCP connection, add an X-Forwarded-For header, send the backend's response back to the client, and close cleanly.
- Return 502 Bad Gateway if the backend can't be reached and 504 Gateway Timeout if it doesn't answer in time (configurable timeout).
- Read backend addresses from a config file.

Tests: parser tests (normal request, split across chunks, missing Content-Length, malformed line, large body), a round-robin test, and an integration test that starts backends and the LB and checks requests are spread evenly.

Done when: `curl localhost:8080/` repeatedly returns responses from backends in rotation, and all tests pass. Update CONCEPTS.md (TCP byte stream, sockets API, HTTP message format, reverse proxy, 502 vs 504). Commit, then stop and summarise.
