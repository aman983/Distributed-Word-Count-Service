"""Put at least one .txt file into this directory for the servers to search.

Run it on your own machine (not in a container) before the first
`docker compose up`:

    python3 data/fetch_corpus.py

It tries to download a public-domain book from Project Gutenberg, which is what
the assignment suggests. If your network blocks that, pass --generate to build a
synthetic corpus of comparable size instead -- the measurements stay valid,
only the words are less interesting.

    python3 data/fetch_corpus.py --generate
"""

import argparse
import os
import random
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# Public-domain texts. Mirrors are tried in order.
SOURCES = {
    "mobydick.txt": [
        "https://www.gutenberg.org/cache/epub/2701/pg2701.txt",
        "https://www.gutenberg.org/files/2701/2701-0.txt",
    ],
    "warandpeace.txt": [
        "https://www.gutenberg.org/cache/epub/2600/pg2600.txt",
    ],
}

WORDS = (
    "whale sea ship captain water night white man time hand eye head old great "
    "long little life day world light dark wind wave boat deck sail storm "
    "island shore fish bone iron rope fire heart blood voice word thought "
    "dream death god king house door window road city river mountain silence "
    "harbour rain cloud sun moon star sand rock forest bird stone bridge"
).split()


def download(name, urls):
    for url in urls:
        try:
            print(f"  trying {url}")
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (ADS lab assignment)"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            path = os.path.join(HERE, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            print(f"  OK  {name}  ({len(body):,} chars)")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {exc}")
    return False


def generate(name="synthetic.txt", target_chars=1_200_000, seed=7):
    """Build a Zipf-ish synthetic corpus so keyword frequencies vary realistically."""
    rng = random.Random(seed)
    weights = [1.0 / (i + 1) for i in range(len(WORDS))]
    out = []
    size = 0
    while size < target_chars:
        sentence = " ".join(rng.choices(WORDS, weights=weights,
                                        k=rng.randint(8, 22)))
        line = sentence.capitalize() + "."
        out.append(line)
        size += len(line) + 1
        if len(out) % 40 == 0:
            out.append("")
            size += 1
    path = os.path.join(HERE, name)
    body = "\n".join(out)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    print(f"  OK  {name}  ({len(body):,} chars, generated)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true",
                    help="skip the download and build a synthetic corpus")
    ap.add_argument("--all", action="store_true",
                    help="download every listed book, not just the first")
    args = ap.parse_args()

    if args.generate:
        generate()
        return

    wanted = list(SOURCES.items()) if args.all else [list(SOURCES.items())[0]]
    got_any = False
    for name, urls in wanted:
        print(f"fetching {name} ...")
        got_any |= download(name, urls)

    if not got_any:
        print("\nDownload failed (blocked network?). Generating a synthetic "
              "corpus instead.")
        generate()

    txts = [f for f in os.listdir(HERE) if f.endswith(".txt")]
    print(f"\ndata/ now contains: {', '.join(sorted(txts)) or '(nothing)'}")
    if not txts:
        sys.exit(1)


if __name__ == "__main__":
    main()
