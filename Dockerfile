# Single image used for the client, the servers and (later) the load balancer.
# The assignment's manual suggests python:3.9-slim-buster, but Debian Buster is
# end-of-life and `apt-get update` fails on it. python:3.11-slim is the modern
# equivalent and works identically for this lab.
FROM python:3.11-slim

WORKDIR /app

# Small OS-level extras: procps gives us `ps`, curl is handy for debugging.
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# Source is also bind-mounted in docker-compose.yml so that editing a .py file
# on the host takes effect on the next container start without a rebuild.
COPY src/ /app/src/

ENV PYTHONUNBUFFERED=1

# Default command: a shell that never exits, so a container started from this
# image never dies by accident. docker-compose.yml overrides it per service.
CMD ["/bin/bash", "-c", "while true; do sleep 3600; done"]
