#!/usr/bin/env python3
"""River-spot generator for the NN warm-start dataset (#59, stage 1).

Samples river spots from a parametrised family (board, ranges, pot/stack, bet
menu), solves each to a target exploitability with the solver over UPI, and
stores the per-node training records via `dump_train`, gzipped. Provenance for
every spot (full sampling parameters + the exact UPI script hash) goes to a
manifest shard, merged at the end into <out>/manifest.jsonl.

Resumable: a spot whose output file already exists is skipped, so re-running
with the same --out and --seed continues where it stopped. Interrupting with
Ctrl-C is safe; an in-flight spot may leave a truncated file that is deleted on
the next run (size-0 and missing-manifest files are re-done).

Usage:
  python3 -I ml/gen_river.py --solver ./solver --count 100 --workers 4 --out /path/data
  python3 -I ml/gen_river.py --solver ./solver --fixture   # write ml/tests/fixture.wsdt
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TARGET_FRACTION = 0.003          # 0.3% of the pot, per issue #59 stage 1
MAX_SOLVE_SECONDS = 120
# Bet sizes as fractions of the starting pot, raise sizes as multipliers over the
# wager faced; add_line tokens are total wagers in CHIPS (cf. the 25bb fixture:
# pot 40 -> ... 26 63 129 230-with-stack-230). Every line is strictly increasing
# and stays at or below the effective stack.
BET_FRACS = [0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.5, 2.0]
RAISE_MULTS = [2.0, 2.5, 3.0]

RANKS = "23456789TJQKA"
SUITS = "cdhs"
NUM_CARDS = 52
COMBOS = [(hi, lo) for hi in range(1, NUM_CARDS) for lo in range(hi)]  # Pio order


def card_str(c: int) -> str:
    return RANKS[c >> 2] + SUITS[c & 3]


def combo_index(a: int, b: int) -> int:
    hi, lo = max(a, b), min(a, b)
    return hi * (hi - 1) // 2 + lo


def sample_spot(rng: random.Random) -> dict:
    board = rng.sample(range(NUM_CARDS), 5)
    blocked = set(board)
    avail = [ci for ci, (hi, lo) in enumerate(COMBOS) if hi not in blocked and lo not in blocked]
    pot = rng.choice([20, 30, 40, 60, 80, 100, 140, 200])
    stack = max(2, round(pot * rng.uniform(0.8, 3.0)))

    def wagers() -> list[int]:
        ws = sorted({max(1, round(pot * f)) for f in rng.sample(BET_FRACS, rng.choice([1, 1, 2]))})
        return [w for w in ws if w < stack]

    def raise_after(w: int) -> int | None:
        cand = [r for r in (round(w * g) for g in RAISE_MULTS) if w < r <= stack]
        return rng.choice(cand) if cand else None

    lines = []
    for w in wagers()[:2]:              # OOP leads
        line = [w]
        if (r := raise_after(w)) is not None:
            line.append(r)
        lines.append(line)
    for w in wagers()[:2]:              # IP bets after an OOP check
        line = [0, w]
        if (r := raise_after(w)) is not None:
            line.append(r)
        lines.append(line)
    if not lines:
        lines = [[0]]                   # stack too shallow for any listed bet: check-down
    return {
        "board": board,
        "combos0": sorted(rng.sample(avail, rng.randint(40, 250))),
        "combos1": sorted(rng.sample(avail, rng.randint(40, 250))),
        "pot": pot,
        "stack": stack,
        "lines": lines,
    }


def spot_script(spot: dict, dump_path: str | None) -> str:
    lines = []
    for p, key in (("OOP", "combos0"), ("IP", "combos1")):
        weights = [0.0] * 1326
        for ci in spot[key]:
            weights[ci] = 1.0
        lines.append(f"set_range {p} " + " ".join(f"{w:g}" for w in weights))
    lines.append("set_board " + "".join(card_str(c) for c in spot["board"]))
    lines.append(f"set_pot 0 0 {spot['pot']}")
    lines.append(f"set_eff_stack {spot['stack']}")
    for line in spot["lines"]:
        lines.append("add_line " + " ".join(str(t) for t in line))
    lines += [
        "set_threads 1",
        "build_tree",
        f"set_accuracy {TARGET_FRACTION} fraction",
        "set_info_freq 25",
        f"go {MAX_SOLVE_SECONDS} seconds",
        "wait_for_solver",
    ]
    if dump_path:
        lines.append(f"dump_train {dump_path}")
    lines.append("exit")
    return "\n".join(lines) + "\n"


def spot_key(script: str) -> str:
    return hashlib.sha1(script.encode()).hexdigest()[:16]


def run_spot(exe: str, spot: dict, raw_path: str) -> dict:
    """Solve one spot; returns the manifest row. Raises on solver failure."""
    script = spot_script(spot, raw_path)
    t0 = time.monotonic()
    proc = subprocess.run([exe], input=script, capture_output=True, text=True,
                          cwd=ROOT, timeout=MAX_SOLVE_SECONDS + 60)
    seconds = time.monotonic() - t0
    out = proc.stdout
    err = next((l for l in out.splitlines() if l.startswith("ERROR")), None)
    if proc.returncode != 0 or err:
        raise RuntimeError(err or f"solver exited {proc.returncode}")
    converged = "required accuracy reached" in out
    expl = [float(l.split(":")[1]) for l in out.splitlines() if l.startswith("Exploitable for:")]
    row = {
        "key": spot_key(spot_script(spot, None)),
        "board": [card_str(c) for c in spot["board"]],
        "pot": spot["pot"],
        "stack": spot["stack"],
        "k0": len(spot["combos0"]),
        "k1": len(spot["combos1"]),
        "lines": spot["lines"],
        "seconds": round(seconds, 3),
        "converged": converged,
        "final_expl": expl[-1] if expl else None,
    }
    return row


def worker(wid: int, exe: str, rng: random.Random, todo: list[int], out_dir: str,
           rows: list[dict], lock: threading.Lock, errors: list[str]) -> None:
    shard = os.path.join(out_dir, f"manifest.part{wid}.jsonl")
    with open(shard, "a") as mf:
        for i in todo:
            spot = sample_spot(rng)
            key = spot_key(spot_script(spot, None))
            gz_path = os.path.join(out_dir, key + ".wsdt.gz")
            raw_path = os.path.join(out_dir, key + ".wsdt")
            if os.path.exists(gz_path):
                continue
            if os.path.exists(raw_path):
                os.remove(raw_path)  # interrupted earlier run
            try:
                row = run_spot(exe, spot, raw_path)
                with open(raw_path, "rb") as fin, gzip.open(gz_path, "wb", compresslevel=6) as fout:
                    fout.write(fin.read())
                os.remove(raw_path)
                row["bytes"] = os.path.getsize(gz_path)
                with lock:
                    rows.append(row)
                mf.write(json.dumps(row) + "\n")
                mf.flush()
            except Exception as e:  # solver crash/timeout: log and continue
                for p in (raw_path, gz_path):
                    if os.path.exists(p):
                        os.remove(p)
                with lock:
                    errors.append(f"worker{wid} spot{i} ({key}): {e}")


FIXTURE_SPOT = {
    "board": [48, 40, 20, 4, 34],               # Ac Qc 7c 3c Th
    "combos0": None,  # filled from seed below
    "combos1": None,
    "pot": 40,
    "stack": 95,
    "lines": [[20, 50], [0, 30, 75]],
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--solver", default=os.path.join(ROOT, "solver"))
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20261102)
    ap.add_argument("--out", default=os.path.expanduser("~/wsolver-data/river_v1"))
    ap.add_argument("--fixture", action="store_true",
                    help="write the small committed test fixture instead of generating")
    args = ap.parse_args(argv)

    if args.fixture:
        rng = random.Random(7)
        spot = dict(FIXTURE_SPOT)
        avail = [ci for ci, (hi, lo) in enumerate(COMBOS)
                 if hi not in set(spot["board"]) and lo not in set(spot["board"])]
        spot["combos0"] = sorted(rng.sample(avail, 48))
        spot["combos1"] = sorted(rng.sample(avail, 56))
        dest = os.path.join(HERE, "tests", "fixture.wsdt")
        row = run_spot(args.solver, spot, dest)
        print(f"wrote {dest} ({os.path.getsize(dest)} bytes); {row}")
        return 0

    if not os.path.exists(args.solver):
        print(f"no solver at {args.solver}; run make first", file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)

    # Deterministic per-worker streams derived from --seed.
    workers_rng = [random.Random((args.seed, w)) for w in range(args.workers)]
    chunks = [list(range(i, args.count, args.workers)) for i in range(args.workers)]
    rows: list[dict] = []
    errors: list[str] = []
    lock = threading.Lock()
    threads = [threading.Thread(target=worker,
                                args=(w, args.solver, workers_rng[w], chunks[w],
                                      args.out, rows, lock, errors))
               for w in range(args.workers)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Merge shards written in any earlier (interrupted) run of this directory too.
    merged = os.path.join(args.out, "manifest.jsonl")
    seen = set()
    with open(merged, "w") as out:
        for f in sorted(os.listdir(args.out)):
            if f.startswith("manifest.part") and f.endswith(".jsonl"):
                with open(os.path.join(args.out, f)) as fin:
                    for line in fin:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # interrupted write from an earlier run
                        if row["key"] in seen:
                            continue
                        seen.add(row["key"])
                        out.write(json.dumps(row) + "\n")
    print(f"done: {len(seen)} spots in {time.monotonic() - t0:.0f}s -> {args.out} "
          f"({len(errors)} errors)", file=sys.stderr)
    for e in errors[:5]:
        print("  " + e, file=sys.stderr)
    return 0 if len(seen) >= args.count * 0.98 else 1


if __name__ == "__main__":
    raise SystemExit(main())
