"""RPyC word-count server.

One of the N replicas of the service. Each replica:
  * holds the corpus in memory (the texts live in ./data, mounted read-only),
  * answers word_count(keyword, text_ref) requests over RPyC,
  * caches every answer in a shared Redis instance,
  * reports its own identity so the client can see which replica served it,
    which is how Phase 3 demonstrates that the load balancer spreads requests.

Run:  python /app/src/server.py
Env:  SERVER_ID, RPYC_HOST, RPYC_PORT, DATA_DIR, REDIS_HOST, REDIS_PORT
"""

import json
import os
import socket
import sys
import threading
import time

import rpyc
from rpyc.utils.server import ThreadedServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wordcount import (  # noqa: E402
    HOT_KEYWORDS_KEY,
    count_with_cache,
    load_texts,
    make_redis,
    wait_for_redis,
)

SERVER_ID = os.environ.get("SERVER_ID") or socket.gethostname()
RPYC_HOST = os.environ.get("RPYC_HOST", "0.0.0.0")
RPYC_PORT = int(os.environ.get("RPYC_PORT", "18861"))
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

# Optional artificial per-request delay in milliseconds. Useful if your machine
# is fast enough that a single server never saturates: set ARTIFICIAL_DELAY_MS
# to make the need for load balancing visible. Leave at 0 for real numbers.
ARTIFICIAL_DELAY_MS = float(os.environ.get("ARTIFICIAL_DELAY_MS", "0"))

TEXTS = {}
RDB = None
_started_at = time.time()
_inflight = 0
_inflight_lock = threading.Lock()


class WordCountService(rpyc.Service):
    """The remote object clients talk to.

    Methods are returned as JSON strings rather than dicts on purpose. RPyC
    proxies non-primitive return values by reference, so returning a dict would
    make every field access a second network round trip and would pollute the
    latency measurement. A JSON string crosses the wire once.
    """

    def on_connect(self, conn):
        pass

    def on_disconnect(self, conn):
        pass

    # -- core operation -----------------------------------------------------
    def exposed_word_count(self, keyword, text_ref=None, use_cache=True):
        global _inflight
        with _inflight_lock:
            _inflight += 1
        try:
            keyword = str(keyword)
            text_ref = str(text_ref) if text_ref else next(iter(TEXTS))

            if ARTIFICIAL_DELAY_MS > 0:
                time.sleep(ARTIFICIAL_DELAY_MS / 1000.0)

            try:
                count, cache_hit, server_ms = count_with_cache(
                    RDB, TEXTS, text_ref, keyword, SERVER_ID, bool(use_cache)
                )
            except KeyError as exc:
                return json.dumps({
                    "ok": False,
                    "error": str(exc),
                    "server_id": SERVER_ID,
                    "available_texts": sorted(TEXTS),
                })

            return json.dumps({
                "ok": True,
                "keyword": keyword,
                "text_ref": text_ref,
                "count": count,
                "cache_hit": cache_hit,
                "server_id": SERVER_ID,
                "server_ms": round(server_ms, 3),
            })
        finally:
            with _inflight_lock:
                _inflight -= 1

    # -- introspection / extra queries --------------------------------------
    def exposed_list_texts(self):
        """Names and sizes of the texts this replica can search."""
        return json.dumps({
            "server_id": SERVER_ID,
            "texts": [
                {"text_ref": name, "chars": len(body)}
                for name, body in sorted(TEXTS.items())
            ],
        })

    def exposed_hot_keywords(self, top_n=10):
        """The most frequently requested keywords, from the Redis sorted set."""
        try:
            rows = RDB.zrevrange(HOT_KEYWORDS_KEY, 0, int(top_n) - 1, withscores=True)
            return json.dumps({
                "ok": True,
                "server_id": SERVER_ID,
                "hot_keywords": [{"keyword": k, "requests": int(v)} for k, v in rows],
            })
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": repr(exc)})

    def exposed_stats(self):
        """Per-replica counters, used to show request distribution in Phase 3."""
        try:
            raw = RDB.hgetall(f"stats:{SERVER_ID}") or {}
        except Exception:  # noqa: BLE001
            raw = {}
        return json.dumps({
            "server_id": SERVER_ID,
            "uptime_s": round(time.time() - _started_at, 1),
            "inflight": _inflight,
            "requests": int(raw.get("requests", 0)),
            "cache_hits": int(raw.get("hits", 0)),
            "cache_misses": int(raw.get("misses", 0)),
        })

    def exposed_ping(self):
        """Cheap liveness probe. Phase 4's health checker calls this."""
        return SERVER_ID

    def exposed_load(self):
        """Number of requests currently being processed.

        A least-connections / resource-based load balancer can poll this, though
        the balancer your teammates build may prefer to count connections on its
        own side instead.
        """
        return _inflight

    def exposed_flush_cache(self):
        """Clear the shared cache. Handy for running a cold-cache experiment."""
        try:
            RDB.flushdb()
            return json.dumps({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "error": repr(exc)})


def main():
    global TEXTS, RDB

    TEXTS = load_texts(DATA_DIR)
    if not TEXTS:
        print(f"[{SERVER_ID}] FATAL: no .txt files found in {DATA_DIR}.", flush=True)
        print(f"[{SERVER_ID}] Run `python3 data/fetch_corpus.py` on the host first.",
              flush=True)
        sys.exit(1)

    RDB = make_redis()
    if wait_for_redis(RDB, timeout=60):
        print(f"[{SERVER_ID}] connected to Redis", flush=True)
    else:
        print(f"[{SERVER_ID}] WARNING: Redis unreachable, serving without cache",
              flush=True)

    total_chars = sum(len(t) for t in TEXTS.values())
    print(f"[{SERVER_ID}] loaded {len(TEXTS)} text(s), {total_chars:,} chars: "
          f"{', '.join(sorted(TEXTS))}", flush=True)
    print(f"[{SERVER_ID}] listening on {RPYC_HOST}:{RPYC_PORT}", flush=True)

    server = ThreadedServer(
        WordCountService,
        hostname=RPYC_HOST,
        port=RPYC_PORT,
        protocol_config={
            "allow_public_attrs": True,
            "allow_pickle": False,
            "sync_request_timeout": 60,
        },
    )
    server.start()


if __name__ == "__main__":
    main()
