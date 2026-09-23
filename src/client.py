"""Word-count client.

Sends a (keyword, text_ref) request over RPyC and prints the answer together
with the measured execution latency.

The target host and port come from environment variables, so the *same* client
code is used in Phase 2 (pointing straight at a server) and in Phase 3/4
(pointing at the load balancer) with no change:

    TARGET_HOST=server1 TARGET_PORT=18861   # Phase 2
    TARGET_HOST=lb      TARGET_PORT=18870   # Phase 3 and 4

Examples
--------
    python /app/src/client.py --keyword whale
    python /app/src/client.py --keyword whale --repeat 5
    python /app/src/client.py --texts
    python /app/src/client.py --hot 10
    python /app/src/client.py --stats
"""

import argparse
import json
import os
import statistics
import sys
import time

import rpyc

DEFAULT_HOST = os.environ.get("TARGET_HOST", "server1")
DEFAULT_PORT = int(os.environ.get("TARGET_PORT", "18861"))


def connect(host, port, timeout=30):
    """Open one RPyC connection.

    A fresh connection per request is deliberate. RPyC connections are
    long-lived and stateful; if the client kept one open, every request would
    ride the same TCP stream and the load balancer would have no opportunity to
    route requests to different replicas. Connecting per request is what makes
    the Phase 3 distribution experiment meaningful, and the connection set-up
    cost is part of the execution latency the assignment asks us to report.
    """
    return rpyc.connect(
        host,
        port,
        config={
            "allow_public_attrs": True,
            "sync_request_timeout": timeout,
        },
    )


def word_count(host, port, keyword, text_ref=None, use_cache=True):
    """Perform one request and return (payload_dict, latency_ms).

    Execution latency is measured exactly as the assignment defines it: from
    just before the request leaves the client to just after the response has
    been received on the client side.
    """
    started = time.perf_counter()
    conn = connect(host, port)
    try:
        raw = conn.root.word_count(keyword, text_ref, use_cache)
        payload = json.loads(raw)
    finally:
        latency_ms = (time.perf_counter() - started) * 1000.0
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return payload, latency_ms


def _simple_call(host, port, method, *args):
    conn = connect(host, port)
    try:
        return json.loads(getattr(conn.root, method)(*args))
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Word count service client")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help="server or load balancer host (default: $TARGET_HOST)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="target port (default: $TARGET_PORT)")
    ap.add_argument("--keyword", "-k", help="keyword to count")
    ap.add_argument("--text", "-t", default=os.environ.get("TEXT_REF"),
                    help="text reference (file name on the server)")
    ap.add_argument("--repeat", "-n", type=int, default=1,
                    help="send the request this many times (shows cache effect)")
    ap.add_argument("--texts", action="store_true", help="list available texts")
    ap.add_argument("--hot", type=int, metavar="N",
                    help="show the N most requested keywords")
    ap.add_argument("--stats", action="store_true",
                    help="show the serving replica's counters")
    ap.add_argument("--flush-cache", action="store_true",
                    help="clear the Redis cache (cold-cache experiments)")
    args = ap.parse_args()

    target = f"{args.host}:{args.port}"

    if args.texts:
        print(json.dumps(_simple_call(args.host, args.port, "list_texts"), indent=2))
        return
    if args.hot:
        print(json.dumps(_simple_call(args.host, args.port, "hot_keywords", args.hot),
                         indent=2))
        return
    if args.stats:
        print(json.dumps(_simple_call(args.host, args.port, "stats"), indent=2))
        return
    if args.flush_cache:
        print(json.dumps(_simple_call(args.host, args.port, "flush_cache"), indent=2))
        return

    if not args.keyword:
        ap.error("--keyword is required (or use --texts / --hot / --stats)")

    print(f"target={target}  keyword='{args.keyword}'  text={args.text or '(default)'}")
    latencies = []
    for i in range(args.repeat):
        try:
            payload, latency_ms = word_count(args.host, args.port,
                                             args.keyword, args.text)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i + 1}] request failed: {exc!r}", file=sys.stderr)
            continue

        latencies.append(latency_ms)
        if not payload.get("ok"):
            print(f"  [{i + 1}] error: {payload.get('error')}  "
                  f"(available: {payload.get('available_texts')})", file=sys.stderr)
            continue

        print(f"  [{i + 1}] count={payload['count']:<7} "
              f"served_by={payload['server_id']:<10} "
              f"cache={'HIT ' if payload['cache_hit'] else 'MISS'} "
              f"server={payload['server_ms']:.2f} ms  "
              f"end_to_end={latency_ms:.2f} ms")

    if len(latencies) > 1:
        print(f"  --> min {min(latencies):.2f} ms | "
              f"mean {statistics.mean(latencies):.2f} ms | "
              f"max {max(latencies):.2f} ms")


if __name__ == "__main__":
    main()
