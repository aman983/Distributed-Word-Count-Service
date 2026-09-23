"""Load generator and latency measurement harness (Phase 2 part 3, Phase 3 part 8).

Drives the service at several fixed request rates and records the execution
latency of every request, then writes:

  results/<label>_raw.csv       one row per request
  results/<label>_summary.csv   one row per rate (avg, p50, p95, p99, ...)
  results/<label>_avg.png       figure 1: average latency vs. request rate
  results/<label>_p99.png       figure 2: 99th-percentile latency vs. rate
  results/<label>_dist.csv      requests served per replica (Phase 3 evidence)

This is an OPEN-LOOP generator: request i is scheduled to depart at
t0 + i/rate regardless of whether earlier requests have come back. That matters.
A closed-loop generator (send, wait, send again) silently throttles itself when
the server slows down, so the latency curve stays flat and the system looks like
it scales when it does not. Open-loop is what exposes saturation.

Examples
--------
    # Phase 2 baseline, one server
    python /app/src/benchmark.py --label phase2_1server

    # Phase 3, through the load balancer
    TARGET_HOST=lb TARGET_PORT=18870 \
      python /app/src/benchmark.py --label phase3_roundrobin

    # custom rates and duration
    python /app/src/benchmark.py --rates 10 20 30 40 50 --duration 15
"""

import argparse
import csv
import json
import os
import random
import statistics
import string
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import matplotlib
matplotlib.use("Agg")  # no display inside the container
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from client import connect, word_count  # noqa: E402

RESULTS_DIR = os.environ.get("RESULTS_DIR", "/app/results")

# A pool of ordinary English words. With --mode pool the generator draws from
# this list, so the run contains a realistic mixture of cache hits and misses.
KEYWORD_POOL = [
    "whale", "sea", "ship", "captain", "water", "night", "white", "man",
    "time", "hand", "eye", "head", "old", "great", "long", "little", "life",
    "day", "world", "light", "dark", "wind", "wave", "boat", "deck", "sail",
    "storm", "island", "shore", "fish", "bone", "iron", "rope", "fire",
    "heart", "blood", "voice", "word", "thought", "dream", "death", "god",
    "king", "house", "door", "window", "road", "city", "river", "mountain",
]


def random_keyword(n=12):
    """A keyword that is certain not to be in the cache (and not in the text)."""
    return "zz" + "".join(random.choices(string.ascii_lowercase, k=n))


def pick_keyword(mode, i, rate):
    if mode in ("single", "nocache"):
        return KEYWORD_POOL[0] if mode == "single" else random.choice(KEYWORD_POOL)
    if mode == "unique":
        return random_keyword()
    if mode == "cold":
        # Deterministic but never repeated *within a run*. The rate is part of
        # the keyword on purpose: without it, the second rate would replay the
        # first rate's keywords and hit the cache, so the "always a cache miss"
        # guarantee would quietly break for every rate after the first.
        return f"zzcold{rate}_{i}"
    return random.choice(KEYWORD_POOL)  # "pool"


def run_rate(host, port, rate, duration, mode, text_ref, warmup, use_cache=True):
    """Send `rate` requests per second for `duration` seconds. Returns rows."""
    total = int(rate * duration)
    interval = 1.0 / rate
    rows = []
    rows_lock = threading.Lock()

    # Enough workers that a slow server does not itself become the bottleneck
    # that limits the offered rate.
    workers = max(64, min(int(rate * 4), 1024))

    def one_request(idx, scheduled_at):
        # Sleep until this request's scheduled departure time.
        delay = scheduled_at - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        keyword = pick_keyword(mode, idx, rate)
        sent_at = time.perf_counter()
        try:
            payload, latency_ms = word_count(host, port, keyword, text_ref,
                                             use_cache)
            ok = bool(payload.get("ok"))
            row = {
                "rate": rate,
                "index": idx,
                "keyword": keyword,
                "ok": ok,
                "count": payload.get("count", -1),
                "cache_hit": bool(payload.get("cache_hit", False)),
                "server_id": payload.get("server_id", "?"),
                "server_ms": payload.get("server_ms", -1),
                "latency_ms": round(latency_ms, 3),
                "schedule_slip_ms": round((sent_at - scheduled_at) * 1000.0, 3),
                "error": "" if ok else str(payload.get("error", "")),
            }
        except Exception as exc:  # noqa: BLE001
            row = {
                "rate": rate, "index": idx, "keyword": keyword, "ok": False,
                "count": -1, "cache_hit": False, "server_id": "?", "server_ms": -1,
                "latency_ms": round((time.perf_counter() - sent_at) * 1000.0, 3),
                "schedule_slip_ms": round((sent_at - scheduled_at) * 1000.0, 3),
                "error": repr(exc),
            }
        with rows_lock:
            rows.append(row)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Optional warm-up requests: not recorded, they just take the one-off
        # costs (thread creation, Redis connection set-up) out of the numbers.
        if warmup > 0:
            list(pool.map(lambda _: _safe_warm(host, port, text_ref),
                          range(warmup)))

        t0 = time.perf_counter()
        futures = [pool.submit(one_request, i, t0 + i * interval)
                   for i in range(total)]
        for f in futures:
            f.result()

    rows.sort(key=lambda r: r["index"])
    return rows


def _safe_warm(host, port, text_ref):
    try:
        word_count(host, port, "warmup", text_ref)
    except Exception:  # noqa: BLE001
        pass


def percentile(values, q):
    """Nearest-rank percentile. q is a fraction, e.g. 0.99."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(q * len(ordered) + 0.5)) - 1))
    return ordered[k]


def summarise(rows, rate, wall_s):
    """Per-rate statistics, plus two validity flags.

    The flags matter more than the numbers. An open-loop generator aimed above
    the server's capacity produces a queue that grows for the whole run, so the
    "average latency" it reports is really a function of how long the run lasted
    -- double the duration and the average doubles. Such a point is not a
    measurement of latency and must not be plotted as one.
    """
    ok_rows = [r for r in rows if r["ok"]]
    lat = [r["latency_ms"] for r in ok_rows]

    # Saturation: compare the first fifth of the run with the last fifth. In
    # steady state these are equal; under saturation the queue has been growing
    # the whole time and the tail is far worse than the head.
    saturated = False
    growth = 1.0
    if len(lat) >= 25:
        k = max(5, len(lat) // 5)
        head = statistics.mean(lat[:k])
        tail = statistics.mean(lat[-k:])
        growth = tail / head if head > 0 else 1.0
        saturated = growth > 2.5 and tail > 150

    # Generator lag: if requests could not depart on schedule, the offered rate
    # was lower than the nominal one and the x-axis is a lie.
    slips = [r["schedule_slip_ms"] for r in rows]
    slip_p99 = percentile(slips, 0.99) if slips else 0.0

    return {
        "rate": rate,
        "requests_sent": len(rows),
        "requests_ok": len(ok_rows),
        "errors": len(rows) - len(ok_rows),
        "achieved_rps": round(len(ok_rows) / wall_s, 2) if wall_s else 0,
        "cache_hit_pct": round(
            100.0 * sum(r["cache_hit"] for r in ok_rows) / len(ok_rows), 1
        ) if ok_rows else 0.0,
        "avg_ms": round(statistics.mean(lat), 3) if lat else float("nan"),
        "median_ms": round(percentile(lat, 0.50), 3),
        "p95_ms": round(percentile(lat, 0.95), 3),
        "p99_ms": round(percentile(lat, 0.99), 3),
        "max_ms": round(max(lat), 3) if lat else float("nan"),
        "latency_growth": round(growth, 2),
        "slip_p99_ms": round(slip_p99, 2),
        "saturated": saturated,
        "generator_lagged": slip_p99 > 50,
    }


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def make_figure(path, rates, values, ylabel, title, series_label, flags=None):
    """flags[i] is True when that point came from a saturated (invalid) run."""
    flags = flags or [False] * len(rates)
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=160)
    ax.plot(rates, values, marker="o", linewidth=2, color="#2563eb",
            markersize=6, label=series_label, zorder=2)
    bad = [(x, y) for x, y, f in zip(rates, values, flags) if f]
    if bad:
        ax.scatter([x for x, _ in bad], [y for _, y in bad], s=150,
                   facecolors="none", edgecolors="#dc2626", linewidths=2,
                   zorder=3, label="server saturated - not steady state")
        ax.set_xlabel("Offered load (keyword requests per second)\n"
                      "circled points: queue still growing when the run ended",
                      fontsize=9)
    for x, y in zip(rates, values):
        ax.annotate(f"{y:.1f}", (x, y), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=8, color="#334155")
    if not bad:
        ax.set_xlabel("Offered load (keyword requests per second)")

    # A saturated point can be 50x the others, which flattens the whole
    # sub-saturation region into an unreadable line along the x-axis. A log
    # scale keeps both regimes legible in one figure.
    finite = [v for v in values if v == v and v > 0]
    if finite and max(finite) / min(finite) > 20:
        ax.set_yscale("log")
        ylabel += "  -- log scale"

    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=11)
    ax.set_xticks(rates)
    ax.grid(True, which="both", linestyle=":", linewidth=0.7, alpha=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


def main():
    ap = argparse.ArgumentParser(description="Word count load generator")
    ap.add_argument("--host", default=os.environ.get("TARGET_HOST", "server1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("TARGET_PORT", "18861")))
    ap.add_argument("--rates", type=int, nargs="+",
                    default=[50, 70, 90, 110, 130],
                    help="request rates to test (5 values, >=10 apart)")
    ap.add_argument("--duration", type=float, default=10.0,
                    help="seconds of traffic per rate")
    ap.add_argument("--mode",
                    choices=["pool", "unique", "single", "cold", "nocache"],
                    default="nocache",
                    help="keyword selection. nocache (default) = real words "
                         "with real counts, cache bypassed server-side, so "
                         "every request does the full scan; pool = realistic "
                         "hit/miss mixture; unique/cold = synthetic keywords "
                         "that miss the cache but always count 0; single = "
                         "always a cache hit")
    ap.add_argument("--text", default=os.environ.get("TEXT_REF"),
                    help="text reference; default = server's first text")
    ap.add_argument("--label", default="run",
                    help="prefix for the output files")
    ap.add_argument("--warmup", type=int, default=20,
                    help="unrecorded warm-up requests before each rate")
    ap.add_argument("--flush-cache", action="store_true",
                    help="flush Redis before the run (cold-cache experiment)")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds to idle between rates")
    args = ap.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)

    print(f"target      : {args.host}:{args.port}")
    print(f"rates       : {args.rates} req/s")
    print(f"duration    : {args.duration}s per rate")
    print(f"keyword mode: {args.mode}")
    print(f"label       : {args.label}\n")

    if args.flush_cache:
        try:
            conn = connect(args.host, args.port)
            conn.root.flush_cache()
            conn.close()
            print("cache flushed\n")
        except Exception as exc:  # noqa: BLE001
            print(f"cache flush failed: {exc!r}\n")

    all_rows = []
    summaries = []

    for rate in args.rates:
        print(f"[rate {rate:>4} req/s] running {args.duration}s ...", flush=True)
        t_start = time.perf_counter()
        rows = run_rate(args.host, args.port, rate, args.duration,
                        args.mode, args.text, args.warmup,
                        use_cache=(args.mode != "nocache"))
        wall = time.perf_counter() - t_start
        all_rows.extend(rows)
        s = summarise(rows, rate, wall)
        summaries.append(s)
        print(f"  ok={s['requests_ok']}/{s['requests_sent']} "
              f"achieved={s['achieved_rps']} rps  "
              f"avg={s['avg_ms']} ms  p99={s['p99_ms']} ms  "
              f"cache_hits={s['cache_hit_pct']}%")
        if s["errors"]:
            first_err = next((r["error"] for r in rows if not r["ok"]), "")
            print(f"  !! {s['errors']} error(s), first: {first_err[:120]}")
        if s["saturated"]:
            print(f"  !! SATURATED: latency grew {s['latency_growth']}x during "
                  f"the run. The server cannot keep up with {rate} req/s, so "
                  f"the queue never stopped growing.")
            print(f"     avg/p99 here measure queue growth, not latency -- "
                  f"they would change if --duration changed. Do NOT plot this "
                  f"point as a latency measurement.")
        if s["generator_lagged"]:
            print(f"     also: the load generator itself fell behind "
                  f"(p99 schedule slip {s['slip_p99_ms']} ms), so the real "
                  f"offered rate was below {rate} req/s.")
        time.sleep(args.settle)

    prefix = os.path.join(RESULTS_DIR, args.label)

    write_csv(f"{prefix}_raw.csv", all_rows, list(all_rows[0].keys()))
    print(f"\n  wrote {prefix}_raw.csv")
    write_csv(f"{prefix}_summary.csv", summaries, list(summaries[0].keys()))
    print(f"  wrote {prefix}_summary.csv")

    # Per-replica distribution: the evidence that the load balancer spreads load.
    dist = Counter(r["server_id"] for r in all_rows if r["ok"])
    total_ok = sum(dist.values()) or 1
    dist_rows = [
        {"server_id": sid, "requests": n, "share_pct": round(100.0 * n / total_ok, 2)}
        for sid, n in sorted(dist.items())
    ]
    write_csv(f"{prefix}_dist.csv", dist_rows, ["server_id", "requests", "share_pct"])
    print(f"  wrote {prefix}_dist.csv")

    rates = [s["rate"] for s in summaries]
    sat_flags = [s["saturated"] for s in summaries]
    make_figure(f"{prefix}_avg.png", rates, [s["avg_ms"] for s in summaries],
                "Average execution latency (ms)",
                f"Average execution latency vs. offered load ({args.label})",
                "mean", sat_flags)
    make_figure(f"{prefix}_p99.png", rates, [s["p99_ms"] for s in summaries],
                "99th-percentile execution latency (ms)",
                f"Tail (p99) execution latency vs. offered load ({args.label})",
                "p99", sat_flags)

    if any(sat_flags):
        bad = [s["rate"] for s in summaries if s["saturated"]]
        good = [s["rate"] for s in summaries if not s["saturated"]]

        # Under saturation the server is flat out, so the completion rate it
        # sustained IS its throughput capacity -- the most useful number in the
        # whole run. Suggest five rates that straddle it.
        capacity = max(s["achieved_rps"] for s in summaries if s["saturated"])
        step = max(10, int(round(capacity / 5 / 10.0)) * 10)
        suggested = [step * i for i in range(1, 6)]

        print(f"\n{'=' * 72}")
        print(f"WARNING: rate(s) {bad} saturated this server.")
        if good:
            print(f"Capacity is between {max(good)} and {min(bad)} req/s; the "
                  f"saturated run sustained {capacity:.0f} req/s.")
        else:
            print(f"Every rate saturated. The saturated run still sustained "
                  f"{capacity:.0f} req/s, which is the capacity.")
        print("Only non-saturated points are valid latency measurements.")
        print(f"Re-run with rates that straddle capacity:")
        print(f"    --rates {' '.join(str(r) for r in suggested)}")
        print("(The manual explicitly allows rates below 100 req/s.)")
        print("")
        print("Do not discard the saturation result -- it is the direct,")
        print("quantitative argument for why Phase 3 needs a load balancer.")
        print("=" * 72)

    print("\nrequests served per replica:")
    for row in dist_rows:
        print(f"  {row['server_id']:<12} {row['requests']:>6}  ({row['share_pct']}%)")

    with open(f"{prefix}_meta.json", "w", encoding="utf-8") as fh:
        json.dump({
            "label": args.label, "host": args.host, "port": args.port,
            "rates": args.rates, "duration_s": args.duration, "mode": args.mode,
            "text_ref": args.text, "summaries": summaries,
            "distribution": dist_rows,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, fh, indent=2)
    print(f"  wrote {prefix}_meta.json")


if __name__ == "__main__":
    main()
