# Test backends: the stock Python image plus psutil for the heartbeat agent.
# The code itself is mounted read-only by docker-compose.yml, so editing it
# needs no rebuild; only a change to this file does (docker compose build).
FROM python:3.11-slim
RUN pip install --no-cache-dir psutil==7.2.2
