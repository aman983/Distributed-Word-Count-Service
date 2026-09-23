"""Overlay several benchmark runs on one figure.

Phase 3 part 8(b) asks for a comparison of the execution latency of the same
requests with one server (Phase 2) and with three servers (Phase 3), for both
load-balancing algorithms. Run benchmark.py once per configuration with a
different --label, then:

    python /app/src/compare_runs.py \
        --labels phase2_1server phase3_roundrobin phase3_leastconn \
        --out comparison

Produces results/comparison_avg.png and results/comparison_p99.png.
"""

import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTS_DIR = os.environ.get("RESULTS_DIR", "/app/results")
COLORS = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#dc2626", "#0891b2"]
MARKERS = ["o", "s", "^", "D", "v", "P"]


def load_summary(label):
    path = os.path.join(RESULTS_DIR, f"{label}_summary.csv")
    if not os.path.exists(path):
        raise SystemExit(f"missing {path} -- run benchmark.py --label {label} first")
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def plot(metric, ylabel, title, labels, data, out_path):
    fig, ax = plt.subplots(figsize=(7.5, 4.4), dpi=160)
    all_rates = set()
    for i, label in enumerate(labels):
        rows = data[label]
        xs = [int(r["rate"]) for r in rows]
        ys = [float(r[metric]) for r in rows]
        all_rates.update(xs)
        ax.plot(xs, ys, marker=MARKERS[i % len(MARKERS)], linewidth=2,
                markersize=6, color=COLORS[i % len(COLORS)], label=label)
    ax.set_xticks(sorted(all_rates))   # request rates are integers, not 22.5
    ax.set_xlabel("Offered load (keyword requests per second)")

    # Same reasoning as in benchmark.py: a saturated point dwarfs the rest.
    finite = [y for label in labels for y in
              (float(r[metric]) for r in data[label]) if y == y and y > 0]
    if finite and max(finite) / min(finite) > 20:
        ax.set_yscale("log")
        ylabel += "  -- log scale"

    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=11)
    ax.grid(True, which="both", linestyle=":", linewidth=0.7, alpha=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f"wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", nargs="+", required=True,
                    help="benchmark labels to overlay")
    ap.add_argument("--out", default="comparison", help="output file prefix")
    args = ap.parse_args()

    data = {label: load_summary(label) for label in args.labels}
    prefix = os.path.join(RESULTS_DIR, args.out)

    # With one label this is just a re-plot of a single run; with several it is
    # the Phase 3 comparison. Title accordingly instead of always claiming a
    # comparison that may not be there.
    suffix = ("vs. offered load" if len(args.labels) == 1
              else "1 server vs. 3 servers")
    plot("avg_ms", "Average execution latency (ms)",
         f"Average execution latency {suffix}",
         args.labels, data, f"{prefix}_avg.png")
    plot("p99_ms", "99th-percentile execution latency (ms)",
         f"Tail (p99) execution latency {suffix}",
         args.labels, data, f"{prefix}_p99.png")

    # A compact table you can paste into the report.
    print("\nlabel                 rate   avg_ms   p99_ms  achieved_rps  errors")
    for label in args.labels:
        for r in data[label]:
            print(f"{label:<20} {r['rate']:>5} {float(r['avg_ms']):>8.2f} "
                  f"{float(r['p99_ms']):>8.2f} {r['achieved_rps']:>13} "
                  f"{r['errors']:>7}")


if __name__ == "__main__":
    main()
