"""THROWAWAY load balancer -- for testing the client/server only.

>>> This file is NOT the team's Phase 3 deliverable. <<<
Whoever owns the load-balancing part writes src/lb.py and the compose service
switches to it. This placeholder exists so the client and server can be tested
end-to-end (and so the client's `TARGET_HOST=lb` path is exercised) before that
code lands. Delete it before submitting if it is not used.

It documents the contract the real load balancer must satisfy:

  * It is a **byte-stream TCP proxy**, not an RPyC service. The assignment is
    explicit that the balancer must not use RPyC itself: it accepts a TCP
    connection from the client, picks a replica, opens a TCP connection to that
    replica, and pumps bytes in both directions until either side closes. The
    RPyC handshake and protocol happen end-to-end between client and replica;
    the balancer never parses them.
  * Listens on $LB_PORT, backends come from $BACKENDS as "host:port,host:port".
  * Routes per connection. The client opens one connection per request, so
    per-connection routing is per-request routing.
  * Runs periodic health checks and routes only to healthy backends (Phase 4).

Env: LB_PORT, BACKENDS, LB_ALGORITHM (round_robin|least_connections),
     HEALTH_INTERVAL
"""

import asyncio
import itertools
import os
import time

LB_PORT = int(os.environ.get("LB_PORT", "18870"))
ALGORITHM = os.environ.get("LB_ALGORITHM", "round_robin")
HEALTH_INTERVAL = float(os.environ.get("HEALTH_INTERVAL", "2"))
BACKENDS = [
    (h.split(":")[0], int(h.split(":")[1]))
    for h in os.environ.get("BACKENDS", "server1:18861").split(",")
    if h.strip()
]

active = {b: 0 for b in BACKENDS}     # open connections per backend
served = {b: 0 for b in BACKENDS}     # total connections routed per backend
healthy = {b: True for b in BACKENDS}
_rr = itertools.cycle(range(len(BACKENDS)))


def choose_backend():
    live = [b for b in BACKENDS if healthy[b]]
    if not live:
        return None
    if ALGORITHM == "least_connections":
        # Fewest in-flight connections wins; ties broken by fewest served so far
        # so that an idle cluster still spreads traffic instead of pinning it.
        return min(live, key=lambda b: (active[b], served[b]))
    for _ in range(len(BACKENDS)):           # round_robin, skipping dead nodes
        cand = BACKENDS[next(_rr)]
        if healthy[cand]:
            return cand
    return live[0]


async def pump(reader, writer):
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def handle_client(c_reader, c_writer):
    backend = choose_backend()
    if backend is None:
        c_writer.close()
        print("[lb] no healthy backend, dropping connection", flush=True)
        return

    try:
        s_reader, s_writer = await asyncio.wait_for(
            asyncio.open_connection(*backend), timeout=5
        )
    except Exception as exc:  # noqa: BLE001
        healthy[backend] = False
        print(f"[lb] connect to {backend[0]}:{backend[1]} failed ({exc!r}); marked unhealthy",
              flush=True)
        c_writer.close()
        return

    active[backend] += 1
    served[backend] += 1
    try:
        await asyncio.gather(
            pump(c_reader, s_writer),
            pump(s_reader, c_writer),
        )
    finally:
        active[backend] -= 1
        for w in (c_writer, s_writer):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass


async def health_loop():
    """Mark a backend healthy iff a TCP connection to it can be established."""
    while True:
        for b in BACKENDS:
            ok = True
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(*b),
                                              timeout=1.5)
                w.close()
            except Exception:  # noqa: BLE001
                ok = False
            if ok != healthy[b]:
                print(f"[lb] {b[0]}:{b[1]} -> {'HEALTHY' if ok else 'UNHEALTHY'}",
                      flush=True)
            healthy[b] = ok
        await asyncio.sleep(HEALTH_INTERVAL)


async def report_loop():
    while True:
        await asyncio.sleep(10)
        line = "  ".join(
            f"{b[0]}:{b[1]} served={served[b]} active={active[b]} "
            f"{'up' if healthy[b] else 'DOWN'}"
            for b in BACKENDS
        )
        print(f"[lb {time.strftime('%H:%M:%S')}] {line}", flush=True)


async def main():
    print(f"[lb] algorithm={ALGORITHM} backends={BACKENDS} port={LB_PORT}",
          flush=True)
    asyncio.create_task(health_loop())
    asyncio.create_task(report_loop())
    server = await asyncio.start_server(handle_client, "0.0.0.0", LB_PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
