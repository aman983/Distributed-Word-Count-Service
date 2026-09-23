# Load Balancer — interface contract (Phase 3 & 4)

This is what `src/lb.py` has to do to work with the existing client and servers.
Everything the client and servers do is already implemented; you own the box in
the middle.

`src/lb_placeholder.py` is a working throwaway that satisfies this contract in
~150 lines. Read it for reference, then **write your own** — it uses plain
round-robin, which scores zero under the rubric, and it does not belong in the
submission.

---

## 1. The one rule that decides your whole design

> *"The load balancer should **not** implement this using Remote Procedure Calls
> as done in RPyC, because this would be way too inefficient. Instead, you can
> use network sockets (**not** WebSocket) to receive and send messages, as well
> as multithreading or concurrency to handle multiple requests. The client and
> servers should communicate via RPyC, while the load balancer handles their
> interactions at the byte-stream level."* — assignment manual, Phase 3

So the balancer is a **TCP proxy**, not an RPyC service:

1. Accept a TCP connection from the client.
2. Choose a healthy replica using your algorithm.
3. Open a TCP connection to that replica.
4. Copy bytes in both directions until either side closes.
5. Never parse, decode or interpret the RPyC protocol.

The RPyC handshake happens end-to-end between client and replica. The balancer
is deliberately protocol-ignorant — that is what makes it efficient, and it is
what the rubric is checking for.

**Do not** `import rpyc` in `lb.py`. If you find yourself wanting to, something
has gone wrong in the design.

`asyncio` is the natural fit (`asyncio.start_server`, `asyncio.open_connection`)
but threads are fine if you prefer them.

---

## 2. Routing granularity

The client opens **one TCP connection per request** — deliberately, see the
design notes in the main README. So:

> **one connection == one request == one routing decision**

You do not need to demultiplex anything. Pick a backend when the connection
arrives, and that connection belongs to that backend until it closes. Your
"active connections" counter is therefore also your "in-flight requests"
counter, which is exactly what least-connections needs.

---

## 3. Configuration

Read these from the environment. `docker-compose.yml` already sets them.

| Variable | Example | Meaning |
|---|---|---|
| `LB_PORT` | `18870` | Port to listen on |
| `BACKENDS` | `server1:18861,server2:18861,server3:18861` | Comma-separated replicas |
| `LB_ALGORITHM` | `least_connections` | Which algorithm to use |
| `HEALTH_INTERVAL` | `2` | Seconds between health checks |

Selecting the algorithm by environment variable (rather than editing code) is
what lets you run the two Phase 3 comparison benchmarks without a rebuild.

To switch the compose service from the placeholder to your implementation:

```yaml
  lb:
    command: ["python", "-u", "/app/src/lb.py"]    # was lb_placeholder.py
```

---

## 4. The two algorithms

Both must be **dynamic** — the choice must depend on observed runtime state, not
on a fixed rotation. A static algorithm scores **zero** on that rubric row even
if implemented perfectly.

| Algorithm | Dynamic? | Notes |
|---|---|---|
| Round-robin | ❌ **no** | Fixed rotation, ignores load. Not acceptable on its own. |
| Least connections | ✅ yes | Fewest in-flight connections wins |
| Weighted least connections | ✅ yes | As above, weighted by replica capacity |
| Least response time | ✅ yes | Track an EWMA of recent completion times per replica |
| Resource-based | ✅ yes | Poll each replica's load and route to the least loaded |

Pick two. **Least connections** and **least response time** are the natural
pair: both are straightforward from a proxy that already sees connection open
and close events, and they behave differently enough under load to make the
comparison interesting.

**Least connections.** Keep `active[backend]`; increment when you open a
connection, decrement in a `finally` when it closes. Route to the minimum. Break
ties by total-served so an idle cluster still spreads traffic instead of pinning
everything to the first replica — a detail that is easy to miss and shows up
immediately in the distribution CSV as a 100/0/0 split.

**Least response time.** Time each connection from open to close and keep a
per-backend exponentially weighted moving average:
`ewma = alpha * sample + (1 - alpha) * ewma`, with `alpha ≈ 0.2`. Route to the
lowest EWMA. Seed new or recovered backends optimistically (a low initial value)
so they actually get traffic, and consider combining with in-flight count so a
backend that is slow *right now* is not chosen repeatedly before its first slow
response lands.

**Resource-based**, if you prefer it: the servers already expose
`load()` (in-flight request count) and `stats()`. But polling them means an RPyC
call *from the balancer*, which the manual forbids — so you would need a
separate side channel. Least connections gives you the same signal for free.

---

## 5. Health checking (Phase 4)

Requirements from the manual:

- Maintain health status for each replica inside the balancer.
- Detect a failed server and re-route its traffic to healthy ones.
- Check **periodically**, so a recovered server is brought back automatically.
- The system must keep serving requests while a replica is down.

A TCP connect attempt is sufficient and cheap:

```python
try:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=1.5)
    writer.close()
    healthy = True
except Exception:
    healthy = False
```

The servers also expose `ping()` over RPyC, but calling it from the balancer
would violate the no-RPyC rule. A TCP probe tests exactly what matters —
can this replica accept a connection — without touching the protocol.

Things worth getting right:

- **Mark unhealthy on connect failure too**, not only on the periodic probe.
  A replica can die between probes, and the failure shows up as a failed
  connect for a real request.
- **Log every state transition** (`server2 -> UNHEALTHY`). That log is the
  evidence Phase 4 item 11 asks you to show.
- **Never route to an unhealthy backend**, and if all are down, close the client
  connection cleanly rather than hanging.
- **Recovery must be automatic.** The probe loop keeps running, so a replica
  that comes back is re-marked healthy and re-enters rotation with no manual
  step. Phase 4 explicitly tests this.

---

## 6. Testing against the existing code

```bash
docker compose --profile phase3 up -d --build
docker compose logs -f lb
```

**Requests are distributed** (Phase 3 item 8a — screenshot this):

```bash
docker compose exec client sh -c \
    "TARGET_HOST=lb TARGET_PORT=18870 python /app/src/client.py -k whale -n 9"
```

`served_by` should vary across server1/2/3.

**Distribution under real load** — `results/<label>_dist.csv` gives the exact
per-replica split:

```bash
docker compose exec client sh -c \
    "TARGET_HOST=lb TARGET_PORT=18870 python /app/src/benchmark.py \
     --label phase3_leastconn --rates 10 20 30 40 50 --duration 10"
```

**Comparison figures** (Phase 3 item 8b) — run once per algorithm, then:

```bash
docker compose exec client python /app/src/compare_runs.py \
    --labels phase2_1server phase3_leastconn phase3_leastrt --out comparison
```

**Fault tolerance** (Phase 4):

```bash
docker compose stop server2          # inject the failure
docker compose logs --tail 20 lb     # should show server2 -> UNHEALTHY
docker compose exec client sh -c \
    "TARGET_HOST=lb TARGET_PORT=18870 python /app/src/client.py -k sea -n 6"
docker compose start server2         # recovery; should rejoin automatically
```

---

## 7. What "success" looks like

A single server saturates at **≈42 req/s** (see the results table in the main
README). With three replicas you should see capacity scale toward ~3× that, and
the latency knee move from ~40 req/s to somewhere above 100 req/s.

Watch for these in `results/<label>_summary.csv`:

- **`saturated`** — `True` means the queue grew for the whole run and those
  latency numbers are an artifact of `--duration`, not a measurement. The
  harness circles such points in red on the figures. Lower the rates.
- **`generator_lagged`** — the *client* was the bottleneck, so the offered rate
  was below the nominal one. Cross-check `achieved_rps`.
- **`errors`** — must be 0. Anything else means connections are being dropped,
  usually a bug in the proxy's close handling.

If the distribution CSV shows a 100/0/0 split, your tie-breaking is wrong: with
all counters at zero, `min()` returns the first element every time.

---

## 8. Common mistakes

**Not closing both directions.** When either side closes, close the other, or
connections leak and your active-connection counts drift upward forever —
which silently breaks least-connections routing.

**Decrementing the counter in the wrong place.** It must be in a `finally`, or
an exception mid-transfer leaves the count permanently inflated and that replica
never gets chosen again.

**Buffering whole messages.** You cannot know where an RPyC message ends without
parsing the protocol. Stream fixed-size chunks (`read(65536)`) and forward them
as they arrive.

**Forgetting `python -u`.** Without it, your prints sit in a buffer and
`docker compose logs` shows nothing, which makes debugging miserable. The
compose file already sets it — keep it.

**Editing `lb.py` and not restarting.** `src/` is bind-mounted so the file
changes on disk immediately, but the running process holds the old module.
`docker compose restart lb` after every edit.
