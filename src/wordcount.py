"""Shared word-counting logic and Redis cache helpers.

Kept separate from server.py so the counting logic can be unit-tested without
starting an RPyC server, and so the cache key scheme lives in exactly one place.
"""

import os
import re
import time
from typing import Dict, Tuple

import redis

# ---------------------------------------------------------------------------
# Cache key scheme
#
#   wc:<text_ref>:<keyword>   -> string, the cached occurrence count
#   hot:keywords              -> sorted set, keyword -> number of requests
#   hot:texts                 -> sorted set, text_ref -> number of requests
#   stats:<server_id>         -> hash, per-server request/hit/miss counters
# ---------------------------------------------------------------------------

CACHE_PREFIX = "wc"
HOT_KEYWORDS_KEY = "hot:keywords"
HOT_TEXTS_KEY = "hot:texts"

# Time-to-live for a cached count, in seconds. 0 / None means "never expire".
CACHE_TTL = int(os.environ.get("CACHE_TTL", "0"))


def cache_key(text_ref: str, keyword: str) -> str:
    return f"{CACHE_PREFIX}:{text_ref}:{keyword.lower()}"


def make_redis(host: str = None, port: int = None) -> redis.Redis:
    """Create a Redis client backed by a connection pool.

    decode_responses=True makes Redis hand us str instead of bytes, which keeps
    the rest of the code free of .decode() calls.
    """
    host = host or os.environ.get("REDIS_HOST", "redis")
    port = int(port or os.environ.get("REDIS_PORT", "6379"))
    return redis.Redis(
        host=host,
        port=port,
        db=0,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=30,
    )


def wait_for_redis(client: redis.Redis, timeout: float = 30.0) -> bool:
    """Block until Redis answers PING, or until `timeout` seconds have passed.

    docker-compose starts containers in dependency order but does not wait for
    the service inside them to be ready, so the server needs to retry.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            client.ping()
            return True
        except redis.exceptions.RedisError:
            time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Text loading and counting
# ---------------------------------------------------------------------------

def load_texts(data_dir: str) -> Dict[str, str]:
    """Load every *.txt file in `data_dir` into memory, keyed by file name.

    Texts are loaded once at server start-up. Re-reading a multi-megabyte file
    from disk on every request would make the measured latency a property of the
    container's page cache rather than of the service, which is not what Phase 2
    asks us to measure.
    """
    texts = {}
    if not os.path.isdir(data_dir):
        return texts
    for name in sorted(os.listdir(data_dir)):
        if not name.lower().endswith(".txt"):
            continue
        path = os.path.join(data_dir, name)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            texts[name] = fh.read()
    return texts


def count_occurrences(text: str, keyword: str) -> int:
    """Count whole-word, case-insensitive occurrences of `keyword` in `text`.

    Whole-word matching means "sea" does not match inside "season". This is the
    behaviour a user of a word-count service expects, and it is the definition
    we state in the report.
    """
    if not keyword:
        return 0
    pattern = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)
    return sum(1 for _ in pattern.finditer(text))


def count_with_cache(
    rdb: redis.Redis,
    texts: Dict[str, str],
    text_ref: str,
    keyword: str,
    server_id: str,
    use_cache: bool = True,
) -> Tuple[int, bool, float]:
    """Return (count, cache_hit, server_side_ms) for one request.

    On a cache miss the text is scanned and the result is written back to Redis,
    so an identical future request is served from memory. This is the
    "in-memory database to reduce execution latency" requirement of Phase 1.

    `use_cache=False` bypasses the cache for this one request. That exists so the
    benchmark can measure the cost of the actual work using *real* keywords with
    real counts, instead of having to invent keywords that are absent from the
    text just to guarantee a miss.
    """
    started = time.perf_counter()

    if text_ref not in texts:
        raise KeyError(f"unknown text reference '{text_ref}'")

    key = cache_key(text_ref, keyword)
    cache_hit = False
    count = None

    # --- cache lookup -------------------------------------------------------
    if use_cache:
        try:
            cached = rdb.get(key)
            if cached is not None:
                count = int(cached)
                cache_hit = True
        except redis.exceptions.RedisError:
            # A cache outage must degrade latency, not correctness: fall through
            # and compute the answer directly. Worth a sentence in the report.
            pass

    # --- cache miss: do the real work --------------------------------------
    if count is None:
        count = count_occurrences(texts[text_ref], keyword)
        if use_cache:
            try:
                if CACHE_TTL > 0:
                    rdb.setex(key, CACHE_TTL, count)
                else:
                    rdb.set(key, count)
            except redis.exceptions.RedisError:
                pass

    # --- bookkeeping for the "hot keywords" extra query ---------------------
    try:
        pipe = rdb.pipeline(transaction=False)
        pipe.zincrby(HOT_KEYWORDS_KEY, 1, keyword.lower())
        pipe.zincrby(HOT_TEXTS_KEY, 1, text_ref)
        pipe.hincrby(f"stats:{server_id}", "requests", 1)
        pipe.hincrby(f"stats:{server_id}", "hits" if cache_hit else "misses", 1)
        pipe.execute()
    except redis.exceptions.RedisError:
        pass

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return count, cache_hit, elapsed_ms
