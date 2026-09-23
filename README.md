# Distributed Word Count Service

A word-count service built as a distributed system: clients ask how many times a
keyword occurs in a text held on the server, answers are cached in Redis, and
from Phase 3 onward a load balancer spreads requests across three replicas.


---

## Architecture

```
                                            ┌──────────────┐
                              ┌────RPyC────▶│   server1    │──┐
                              │             └──────────────┘  │
  ┌────────┐    RPyC over   ┌─┴──────────┐  ┌──────────────┐  │
  │ client │───byte stream─▶│     lb     │─▶│   server2    │──┼──▶ ┌─────────┐
  └────────┘                │  (sockets) │  └──────────────┘  │    │  Redis  │
                            └─┬──────────┘  ┌──────────────┐  │    │  cache  │
                              │             │   server3    │──┘    └─────────┘
                              └────RPyC────▶└──────────────┘
```

| Component | Role |
|---|---|
| **client** | Sends `(keyword, text_ref)`, measures execution latency client-side |
| **lb** | TCP byte-stream proxy; picks a replica per connection, health-checks them |
| **server1-3** | RPyC service; scans the text, caches the answer in Redis |
| **Redis** | Shared in-memory cache, plus hot-keyword tracking |

The client and the servers speak **RPyC**. The load balancer does **not** — it
forwards raw bytes, so the RPyC handshake happens end-to-end between client and
replica. This is required by the assignment and explained in
[docs/LOAD_BALANCER.md](docs/LOAD_BALANCER.md).

---

## Quick start

**Prerequisites:** Docker **and** Compose v2. Ubuntu's `docker.io` package does
not include Compose — verify both, and see [docs/RUNNING.md](docs/RUNNING.md)
if either check fails.

```bash
docker --version
docker compose version     # a space, not a hyphen; must print v2.x.x
docker ps                  # must not say "permission denied"
```

```bash
git clone <repo-url> && cd ADS_project
python3 data/fetch_corpus.py          # downloads Moby Dick (~1.2 MB)
docker compose up -d --build          # redis + server1 + client
docker compose exec client python /app/src/client.py -k whale -n 3
```

```
[1] count=1228  served_by=server1  cache=MISS  server=21.3 ms  end_to_end=24.8 ms
[2] count=1228  served_by=server1  cache=HIT   server=0.50 ms  end_to_end=2.4 ms
[3] count=1228  served_by=server1  cache=HIT   server=0.47 ms  end_to_end=1.9 ms
```

The MISS-then-HIT pattern is the Redis cache doing its job: a ~21 ms full text
scan becomes a ~0.5 ms lookup.

Three replicas behind the load balancer:

```bash
docker compose --profile phase3 up -d --build
docker compose exec client sh -c \
    "TARGET_HOST=lb TARGET_PORT=18870 python /app/src/client.py -k whale -n 9"
```

`served_by` should cycle across server1/2/3.

---

## Results so far

Phase 2, one server, cache bypassed so every request does a full scan
(`--mode nocache`), 10 s per rate, 1,500 requests, **0 errors**:

| Offered load | Avg latency | p99 latency | Achieved | Status |
|---:|---:|---:|---:|---|
| 10 req/s | 27.7 ms | 35.7 ms | 10.05 req/s | steady state |
| 20 req/s | 27.0 ms | 37.0 ms | 19.97 req/s | steady state |
| 30 req/s | 23.7 ms | 32.3 ms | 29.88 req/s | steady state |
| 40 req/s | 20.9 ms | 31.0 ms | 39.79 req/s | steady state |
| 50 req/s | **1113 ms** | **1893 ms** | **41.97 req/s** | **saturated** |

**A single server saturates at ≈42 req/s.** Raising offered load from 40 to 50
(+25%) raises average latency from 21 ms to 1.1 s — a **53× increase**. Beyond
capacity the request queue grows for the whole run and never reaches steady
state, so those latency figures are a function of run duration, not a property
of the service.

This is the quantitative argument for Phase 3: one server cannot absorb the
load, so the work has to be spread across replicas.

Figures and raw data land in `results/`. The benchmark harness detects
saturation automatically and circles those points on the figures — see
[docs/RUNNING.md](docs/RUNNING.md#reading-the-saturation-warning).

---

## What's done

- Client, server and Redis caching — the full Phase 2 implementation
- Docker Compose cluster: `redis` + `server1` + `client`, with a `phase3`
  profile that adds two more replicas and the balancer
- Benchmark harness: open-loop load generator, avg/p99 figures, per-replica
  distribution CSV, automatic saturation detection
- Phase 2 latency measurements at 10–50 req/s (table above)

## What's left

**Implementation**

- [ ] `src/lb.py` — TCP byte-stream proxy with **two dynamic** algorithms.
      Full spec in [docs/LOAD_BALANCER.md](docs/LOAD_BALANCER.md);
      `src/lb_placeholder.py` is a throwaway reference, **not** the deliverable
- [ ] Health checking in the balancer: detect failed replicas, re-route, and
      bring recovered ones back automatically (Phase 4)

**Experiments**

- [ ] Cache-effect run (`--mode pool`) — nothing currently demonstrates the
      Redis cache under load, and Phase 2 requires caching in an in-memory DB
- [ ] Phase 3 item 8a: screenshot showing requests distributed across replicas
- [ ] Phase 3 item 8b: 1 server vs 3 servers, for **both** algorithms
- [ ] Phase 4: behaviour with a replica down, and after it recovers, for both
      algorithms

**Report**

- [ ] Phase 1: two functional + two non-functional requirements, two
      stakeholders and their roles
- [ ] Phase 1: architecture diagram (client-server) + description
- [ ] Phase 1: trade-off analysis of two other architectural styles
      (Peer-to-Peer / Layered / Publish-Subscribe)
- [ ] Phase 3: updated architecture figure including balancer and replicas
- [ ] Phase 3: argument for why a load balancer is needed — the ~42 req/s
      saturation result above is the evidence
- [ ] Write-up in the IEEE double-column template, max 3 pages of text
- [ ] Code zips: `lab-<groupID>-phase2.zip`, `-phase3.zip`, `-phase4.zip`

---

## Phase checklist

| Phase | Points | Deliverable | Status |
|---|---:|---|---|
| 1 Architectural model | 1.5 | Requirements, stakeholders, diagram, style trade-offs | todo |
| 2 Implementation | 1.0 | RPyC service + cache, avg & p99 latency figures | code done |
| 3 Scalability & LB | 3.5 | 3 replicas, 2 dynamic algorithms, comparison | in progress |
| 4 Fault tolerance | 2.5 | Health checks, failure + recovery experiments | todo |
| All phases | 1.5 | Code zip per phase + individually graded debrief | todo |

Hard constraints from the manual:

- Python + Docker Compose; RPyC between client and servers.
- The load balancer must **not** use RPyC — sockets/asyncio only.
- Both load-balancing algorithms must be **dynamic**. Static round-robin alone
  scores zero.
- Do **not** use Compose's `deploy: replicas` — it assigns random container
  names and adds its own balancer. Replicas are declared as separate services.
- Report in the IEEE double-column template, max 3 pages of text, no style
  changes. Figures and tables don't count toward the limit.

---

## Repository layout

```
.
├── docker-compose.yml       cluster definition; phase3 services behind a profile
├── Dockerfile               one image for client, servers and balancer
├── requirements.txt         rpyc, redis, matplotlib, numpy
├── data/
│   └── fetch_corpus.py      downloads or generates the text(s) to search
├── src/
│   ├── wordcount.py         counting logic + Redis cache helpers
│   ├── server.py            RPyC service (one replica)
│   ├── client.py            one request, latency measured client-side
│   ├── benchmark.py         open-loop load generator → CSVs + figures
│   ├── compare_runs.py      overlays several runs in one figure
│   └── lb_placeholder.py    throwaway balancer, for local testing only
├── docs/
│   ├── RUNNING.md           full setup, benchmarking and troubleshooting guide
│   └── LOAD_BALANCER.md     interface contract for the Phase 3 balancer
├── results/                 CSVs and figures (git-ignored)
└── report/                  IEEE LaTeX report
```

`data/*.txt` and `results/*` are git-ignored: the corpus is downloadable and
measurements are machine-specific, so neither belongs in version control.

---

## Design notes

Four decisions that are easy to get wrong and worth defending in the report:

**Execution latency is measured client-side, around the whole operation**
(connect → call → response), matching the assignment's definition. The server
also reports its own processing time (`server_ms`), so service time can be
separated from connection and queueing overhead.

**The client opens a fresh connection per request.** RPyC connections are
long-lived; if the client held one open, every request would ride the same TCP
stream and the load balancer could not distribute anything.

**The load generator is open-loop** — requests depart on a fixed schedule
whether or not earlier ones have returned. A closed-loop generator throttles
itself when the server slows down, which hides saturation entirely and produces
a flat, reassuring, wrong graph.

**The cache is shared across replicas.** A miss served by server1 becomes a hit
for server2, which maximises hit rate — but makes Redis a single point of
failure and a shared bottleneck. A deliberate trade-off, not an oversight.

---

## Contributing


```bash
git checkout -b <your-feature>
# work, then:
docker compose restart server1      # long-lived processes need a restart
git commit -am "..." && git push -u origin <your-feature>
```

`src/` is bind-mounted into the containers, so editing a file changes it on disk
immediately — but any container running a **long-lived process** (`server1-3`,
`lb`) must be restarted to load it. The `client` container is an idle shell and
never needs one.

Don't commit `results/` or `data/*.txt`.
