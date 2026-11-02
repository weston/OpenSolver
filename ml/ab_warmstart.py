#!/usr/bin/env python3
"""Acceptance A/B for the NN warm start (#59): cold DCFR vs warm-started solve.

Samples held-out river spots with gen_river.sample_spot (a DIFFERENT seed than
the training corpus, so the net has never seen them), solves each to
TARGET_FRACTION of the pot cold and with `warm_start <wsnb> <K> <beta>` for a
list of arms, and compares the number of REAL solve iterations (show_iters
minus the K preload) plus process wall time. Iterations are the primary
metric: they are free of machine timing noise and per-iteration cost is
identical across arms.

Usage:
  python3 -I ml/ab_warmstart.py --wsnb ~/wsolver-data/ckpt/river_v1.wsnb \
      --spots 24 --arms 100:0,300:0,50:200 --json-out /tmp/ab.json

Arm spec: comma-separated K[:beta]; beta defaults to 0 (sum preload only).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gen_river  # noqa: E402


def run_arm(exe: str, script: str) -> dict:
    t0 = time.monotonic()
    p = subprocess.run([exe], input=script, capture_output=True, text=True,
                       cwd=gen_river.ROOT, timeout=gen_river.MAX_SOLVE_SECONDS + 60)
    wall = time.monotonic() - t0
    out = p.stdout
    err = next((l for l in out.splitlines() if l.startswith("ERROR")), None)
    if p.returncode != 0 or err:
        return {"error": err or f"solver exited {p.returncode}", "wall": round(wall, 3)}
    lines = out.splitlines()
    iters = None
    for i, l in enumerate(lines):
        if l == "wait_for_solver ok!" and i + 1 < len(lines) and lines[i + 1].isdigit():
            iters = int(lines[i + 1])
    expl = [float(l.split(":")[1]) for l in lines if l.startswith("Exploitable for:")]
    return {
        "wall": round(wall, 3),
        "iters": iters,
        "converged": "required accuracy reached" in out,
        "final_expl": expl[-1] if expl else None,
    }


def arm_script(spot: dict, wsnb: str | None, k: int, beta: float) -> str:
    script = gen_river.spot_script(spot, None)
    assert script.endswith("exit\n")
    script = script[: -len("exit\n")]
    if wsnb is not None:
        script = script.replace("build_tree\n",
                                f"build_tree\nwarm_start {wsnb} {k} {beta}\n", 1)
    return script + "show_iters\nexit\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wsnb", required=True, help="exported WSNW0001 net")
    ap.add_argument("--solver", default=os.path.join(gen_river.ROOT, "solver"))
    ap.add_argument("--spots", type=int, default=24)
    ap.add_argument("--seed", type=int, default=596159,
                    help="MUST differ from the corpus seed (gen_river --seed)")
    ap.add_argument("--arms", default="100:0,300:0,50:200",
                    help="comma-separated K[:beta] warm arms; cold is always run first")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    if args.seed == 20261102:
        ap.error("seed collides with the training corpus seed")

    rng = random.Random(args.seed)
    spots = [gen_river.sample_spot(rng) for _ in range(args.spots)]
    arms = []
    for tok in args.arms.split(","):
        kb = tok.split(":")
        arms.append((int(kb[0]), float(kb[1]) if len(kb) > 1 else 0.0))
    wsnb = os.path.abspath(os.path.expanduser(args.wsnb))

    rows = []
    for si, spot in enumerate(spots):
        row = {"spot": si,
               "board": [gen_river.card_str(c) for c in spot["board"]],
               "pot": spot["pot"], "stack": spot["stack"],
               "checkdown": spot["lines"] == [[0]], "arms": {}}
        cold = run_arm(args.solver, arm_script(spot, None, 0, 0.0))
        row["arms"]["cold"] = cold
        tag = f"spot {si:3d}/{len(spots)} cold iters={cold.get('iters')}"
        for k, beta in arms:
            name = f"k{k}b{beta:g}"
            r = run_arm(args.solver, arm_script(spot, wsnb, k, beta))
            if "iters" in r and r["iters"] is not None:
                r["iters_real"] = r["iters"] - k  # warm_start preloads K
            row["arms"][name] = r
            tag += f" | {name} real_iters={r.get('iters_real')}"
        print(tag, flush=True)
        rows.append(row)

    print(f"\n{'arm':>10} {'conv':>5} {'med real iters':>15} {'iter ratio':>11} "
          f"{'med wall':>9} {'wall ratio':>11}")
    cold_ok = [r["arms"]["cold"] for r in rows
               if r["arms"]["cold"].get("iters") and r["arms"]["cold"].get("converged")]
    cold_iters = [a["iters"] for a in cold_ok]
    cold_walls = [a["wall"] for a in cold_ok]
    names = ["cold"] + [f"k{k}b{b:g}" for k, b in arms]
    for name in names:
        arms_list = [r["arms"][name] for r in rows if name in r["arms"]]
        conv = sum(1 for a in arms_list if a.get("converged"))
        key = "iters" if name == "cold" else "iters_real"
        iters = [a[key] for a in arms_list if a.get(key) is not None and a.get("converged")]
        walls = [a["wall"] for a in arms_list if a.get("converged")]
        med = statistics.median(iters) if iters else None
        iratio = ""
        if med is not None and name != "cold" and cold_iters:
            iratio = f"{med / statistics.median(cold_iters):.3f}"
        wratio = ""
        if walls and name != "cold" and cold_walls:
            wratio = f"{statistics.median(walls) / statistics.median(cold_walls):.3f}"
        print(f"{name:>10} {conv:>5} {med if med is not None else '-':>15} {iratio:>11} "
              f"{statistics.median(walls) if walls else '-':>9} {wratio:>11}")

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"wsnb": wsnb, "arms": names, "seed": args.seed, "rows": rows}, f, indent=1)
        print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
