#!/usr/bin/env python3
"""Per-command semantic comparison of pio.txt vs ours.txt from compare.py (1326-vectors compared hand by hand)."""
import math, sys, os
sys.path.insert(0, os.path.dirname(__file__))
from compare import split_responses, script_commands

def vec(line):
    return [float(x) for x in line.split()]

def main(results_dir, script, ours="./solver"):
    cmds = script_commands(script, ours)
    pr, _ = split_responses(open(os.path.join(results_dir, "pio.txt")).read())
    orr, _ = split_responses(open(os.path.join(results_dir, "ours.txt")).read())
    for i, cmd in enumerate(cmds[:-1]):
        a, b = pr[i], orr[i]
        al, bl = a.split("\n"), b.split("\n")
        if not (al and len(al[0].split()) in (1326, 169)):
            continue
        print(f"[{i}] {cmd[:70]}")
        for k, (x, y) in enumerate(zip(al, bl)):
            try:
                vx, vy = vec(x), vec(y)
            except ValueError:
                print("   line", k, "non-numeric"); continue
            if len(vx) != len(vy):
                print("   line", k, "length", len(vx), len(vy)); continue
            pat = {}
            worst = 0; worst_i = -1; n = 0
            for j, (p, q) in enumerate(zip(vx, vy)):
                if math.isnan(p) or math.isinf(p):
                    key = f"pio={p} ours={q if q == 0 else 'nonzero'}"
                    pat[key] = pat.get(key, 0) + 1
                    continue
                n += 1
                d = abs(p - q) / max(1.0, abs(p))
                if d > worst: worst, worst_i = d, j
            print(f"   line {k}: finite={n} max_rel_diff={worst:.4g} (hand {worst_i}: pio {vx[worst_i] if worst_i>=0 else '-'} ours {vy[worst_i] if worst_i>=0 else '-'}) special={pat}")

if __name__ == "__main__":
    main(*sys.argv[1:])
