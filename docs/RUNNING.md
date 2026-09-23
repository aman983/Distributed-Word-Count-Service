# Running the service

Full setup, benchmarking and troubleshooting guide. For the project overview see
the [main README](../README.md); for the Phase 3 balancer interface see
[LOAD_BALANCER.md](LOAD_BALANCER.md).

**Contents**

- [One-time setup](#0-one-time-setup-on-your-machine)
- [Phase 2: single server](#1-phase-2-run-the-single-server-service)
- [Reading the saturation warning](#reading-the-saturation-warning)
- [Phase 3/4: replicas + balancer](#2-phase-34-three-replicas-behind-the-load-balancer)
- [Housekeeping](#3-useful-housekeeping)
- [Troubleshooting](#4-troubleshooting)

> **Commands below are wrapped in `sh -c "..."`.** This is not required, but it
> keeps the script's own flags (`-k`, `--rates`, ...) inside a single argument,
> which avoids a class of quoting problems and lets environment variables be set
> inline. The unwrapped form works fine too.

## 0. One-time setup on your machine

1. **Install Docker and Compose v2.** Verify *both*:

   ```bash
   docker --version
   docker compose version     # note: a space, not a hyphen
   ```

   Having `docker` is not enough — Compose v2 is a **separate CLI plugin**, and
   Ubuntu's `docker.io` package does not include it. If the second command
   prints `unknown command: docker compose` (or an error about an unknown
   shorthand flag such as `-d` or `-k`, which is the same problem surfacing
   differently), install it.

   On Ubuntu/Debian:

   ```bash
   sudo apt install -y docker.io docker-compose-v2
   sudo systemctl enable --now docker
   sudo usermod -aG docker $USER
   newgrp docker          # applies the group here; log out/in for other shells
   ```

   If `docker-compose-v2` is not in your repos, install the plugin at user level
   instead — no sudo, works regardless of how Docker was installed:

   ```bash
   mkdir -p ~/.docker/cli-plugins
   curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
     -o ~/.docker/cli-plugins/docker-compose
   chmod +x ~/.docker/cli-plugins/docker-compose
   ```

   (On ARM swap `linux-x86_64` for `linux-aarch64`. With Docker's own apt
   repository, `sudo apt install docker-compose-plugin` is the equivalent —
   that package name does *not* exist in Ubuntu's repositories.)

   Three checks must all pass before continuing:

   ```bash
   docker --version
   docker compose version     # v2.x.x
   docker ps                  # must not say "permission denied"
   ```

   This project needs Compose **v2**. The old `docker-compose` (v1, hyphenated)
   will not work: `docker-compose.yml` here uses the Compose Spec — a top-level
   `name:`, `profiles:`, and `depends_on.condition` — which v1 cannot parse.

   If Docker commands need `sudo` on your machine, prefix every command with
   `sudo`, or add yourself to the `docker` group:
   `sudo usermod -aG docker $USER` then log out and back in.

2. **Get a text to search.** Nothing runs without at least one `.txt` in `data/`:

   ```bash
   python3 data/fetch_corpus.py              # downloads Moby Dick from Gutenberg
   python3 data/fetch_corpus.py --generate   # offline fallback, synthetic ~1.2 MB
   ```

3. You do **not** need Python or any library installed on the host — everything
   runs inside containers. (The exception is `fetch_corpus.py` above, which is
   plain standard-library Python.)

## 1. Phase 2: run the single-server service

```bash
docker compose up -d --build            # redis + server1 + client
docker compose ps                       # all three should be Up
docker compose logs -f server1          # should say "loaded 1 text(s) ... listening"
```

Send a request. Repeating the same one shows the cache working:

```bash
docker compose exec client sh -c "python /app/src/client.py -k whale -n 3"
```

```
[1] count=1685  served_by=server1  cache=MISS  server=21.3 ms  end_to_end=24.8 ms
[2] count=1685  served_by=server1  cache=HIT   server=0.50 ms  end_to_end=2.4 ms
[3] count=1685  served_by=server1  cache=HIT   server=0.47 ms  end_to_end=1.9 ms
```

Other client commands:

```bash
docker compose exec client sh -c "python /app/src/client.py --texts"       # what can be searched
docker compose exec client sh -c "python /app/src/client.py --hot 10"      # hot keywords (extra query)
docker compose exec client sh -c "python /app/src/client.py --stats"       # per-replica counters
docker compose exec client sh -c "python /app/src/client.py --flush-cache"
```

### The Phase 2 measurement (assignment item 3)

Two figures at five rates, lowest ≥ 10 req/s, intervals ≥ 10:

```bash
docker compose exec client sh -c "python /app/src/benchmark.py \
    --label phase2_1server --rates 50 70 90 110 130 --duration 10 --mode cold"
```

Outputs land in `results/` **on your host** (the folder is bind-mounted):

| file | what it is |
|---|---|
| `phase2_1server_avg.png` | **Figure 1** — average execution latency vs. rate |
| `phase2_1server_p99.png` | **Figure 2** — 99th-percentile (tail) latency vs. rate |
| `phase2_1server_summary.csv` | per-rate numbers, plus the validity columns below |
| `phase2_1server_raw.csv` | one row per request, if you want your own analysis |
| `phase2_1server_dist.csv` | requests per replica (interesting from Phase 3 on) |

Keyword modes:

| mode | keywords | cache | use it for |
|---|---|---|---|
| `nocache` (default) | real words | bypassed server-side | the honest cost of the work, with real counts |
| `pool` | real words | normal | realistic hit/miss mixture |
| `single` | one word | always hits | the cache's best case |
| `cold`/`unique` | synthetic | always misses | forces a miss, but every count is 0 |

Running `nocache`, `pool` and `single` and discussing the difference is easy
extra credit. Prefer `nocache` over `cold` for the headline figures: `cold`
guarantees a cache miss by inventing keywords that are not in the text, so the
scan cost is identical but every screenshot shows `count=0`, which looks broken.

### Reading the saturation warning

Pick rates the server can actually sustain. If you aim above its capacity, the
queue grows for the entire run and the "average latency" you get is really a
function of `--duration` — run it twice as long and the average doubles. Those
points are not latency measurements and must not be plotted as such.

The harness detects this and tells you:

```
!! SATURATED: latency grew 45.0x during the run. The server cannot keep up
   with 70 req/s, so the queue never stopped growing.
   also: the load generator itself fell behind (p99 schedule slip 252 ms), so
   the real offered rate was below 70 req/s.
```

Saturated points are circled in red on the figures, the `saturated`,
`latency_growth` and `slip_p99_ms` columns land in the summary CSV, and the run
ends by suggesting rates that straddle the measured capacity. Two independent
checks:

* **`saturated`** — mean latency of the last fifth of the run is >2.5x the first
  fifth. The server is the bottleneck.
* **`generator_lagged`** — p99 schedule slip >50 ms. The *client* is the
  bottleneck, so the x-axis overstates the load actually offered. Cross-check
  with `achieved_rps`: if it is far below `rate`, that rate is fiction.

Saturation is not a failed experiment. It is the quantitative argument for why
Phase 3 needs a load balancer — report it, with the capacity number, and then
re-run at valid rates for the latency curves. The manual explicitly permits
rates below 100 req/s.

## 2. Phase 3/4: three replicas behind the load balancer

```bash
docker compose --profile phase3 up -d --build
docker compose ps        # redis, server1, server2, server3, lb, client
```

Point the client at the balancer instead of the server — no code change, just
two environment variables set inside the quoted command:

```bash
docker compose exec client sh -c \
    "TARGET_HOST=lb TARGET_PORT=18870 python /app/src/client.py -k whale -n 9"
```

You should see `served_by` cycling over server1/2/3 — that is the screenshot
Phase 3 item 8(a) asks for. `docker compose logs -f lb` gives a second view.

Benchmark through the balancer, once per algorithm:

```bash
# edit LB_ALGORITHM in docker-compose.yml between the two runs, then
# `docker compose --profile phase3 up -d lb` to apply it
docker compose exec client sh -c \
    "TARGET_HOST=lb TARGET_PORT=18870 python /app/src/benchmark.py --label phase3_roundrobin --mode cold"
docker compose exec client sh -c \
    "TARGET_HOST=lb TARGET_PORT=18870 python /app/src/benchmark.py --label phase3_leastconn --mode cold"

docker compose exec client sh -c "python /app/src/compare_runs.py \
    --labels phase2_1server phase3_roundrobin phase3_leastconn --out comparison"
```

`comparison_avg.png` and `comparison_p99.png` are exactly the 1-server vs.
3-server comparison Phase 3 item 8(b) asks for.

### Fault tolerance (Phase 4)

```bash
docker compose stop server2          # introduce the failure
docker compose logs --tail 20 lb     # health checker marks server2 UNHEALTHY
docker compose exec client sh -c \
    "TARGET_HOST=lb TARGET_PORT=18870 python /app/src/client.py -k sea -n 6"
docker compose start server2         # recovery: server2 comes back into rotation
```

Only server1 and server3 should answer while server2 is down. `docker compose ps`
before/after is the "status of the servers using docker" evidence item 11 asks for.

## 3. Useful housekeeping

```bash
docker compose down                  # stop everything (keep images)
docker compose down -v               # also drop the Redis data
docker compose --profile phase3 down # stop the phase3 services too
docker compose build --no-cache      # rebuild after editing requirements.txt
docker image prune                   # remove dangling images
docker network prune                 # if a stale ads-net gets in the way
```

Python files are bind-mounted, so after editing `src/*.py` you only need
`docker compose restart server1` (or `up -d`), not a rebuild.

## 4. Troubleshooting

| symptom | cause and fix |
|---|---|
| `unknown shorthand flag: 'k' in -k` (or `'d' in -d`) | Compose v2 is not installed. `docker` does not recognise `compose`, keeps parsing, and trips on the next flag. Same fix as the row below — it is not a quoting problem. |
| `docker: unknown command: docker compose` | Compose v2 plugin missing — see setup step 1. `docker.io` alone does not provide it. |
| `Command 'docker' not found` | Docker itself is not installed: `sudo apt install -y docker.io docker-compose-v2`. |
| `permission denied while trying to connect to the Docker daemon socket` | You are not in the `docker` group: `sudo usermod -aG docker $USER`, then `newgrp docker` or log out and back in. |
| `FATAL: no .txt files found in /app/data` | Run `python3 data/fetch_corpus.py` on the host, then `docker compose restart server1`. |
| Client hangs or times out | Check `docker compose ps` — if `server1` is restarting, read `docker compose logs server1`. |
| Latency curve is suspiciously flat | One server never saturated. Raise the rates, or set `ARTIFICIAL_DELAY_MS: "20"` on the server services (and say so in the report). |
| `achieved_rps` far below the target rate | The *generator* was the bottleneck, not the server. Lower the rates; the numbers above that point are not meaningful. |
| Latency in the thousands of ms, climbing all run | The server is saturated — see the saturation section above. Lower the rates. |
| Every `count` is 0 | You used `--mode cold`/`unique`, whose keywords are not in the text. Use `--mode nocache` for real counts. |

## 5. See also

* [Repository layout](../README.md#repository-layout) and [design notes](../README.md#design-notes) — in the main README.
* [Load balancer contract](LOAD_BALANCER.md) — for whoever builds `src/lb.py`.
