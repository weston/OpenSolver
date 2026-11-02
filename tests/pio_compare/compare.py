#!/usr/bin/env python3
"""Run one UPI script against OpenUPI-Solver and a real PioSOLVER, then diff each response.

Usage:
  python3 tests/pio_compare/compare.py SCRIPT [--out DIR] [--ours ./solver] [--pio-only|--ours-only]

The PioSOLVER side runs over SSH (black-box: only UPI commands are sent). Configure with env vars:
  PIO_SSH   ssh destination              (required unless PIO_LOCAL=1, e.g. user@windows-host)
  PIO_KEY   ssh private key              (default: ~/.ssh/pio_pc_ed25519)
  PIO_EXE   solver path on the Windows box (default: C:\\PioSOLVER\\PioSOLVER3-edge-znver5.exe)
  PIO_LOCAL =1 to run PIO_EXE directly, when this script runs on the Pio machine itself

Each script line that is a command gets one response. "set_end_string END" is prepended automatically.

By default numeric responses match within --tol, which hides pure formatting differences (digits,
trailing spaces, blank lines). --exact counts only byte-identical responses as equal and labels the
rest "within tolerance, bytes differ" when they agree within --tol; that is how #125 was found.
"""
import argparse
import os
import re
import subprocess
import sys

END = "END"
NUM = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")


def range_to_floats(ours_exe, text):
    """PioSOLVER's set_range only takes 1326 numbers; convert range text with our parser."""
    p = subprocess.run([ours_exe], input=f"set_end_string END\nset_range OOP {text}\nshow_range OOP\nexit\n",
                       capture_output=True, text=True, timeout=60)
    resp, _ = split_responses(p.stdout)
    if not resp[1].startswith("set_range ok"):
        raise SystemExit(f"bad range text: {text}: {resp[1]}")
    return resp[2]


def script_commands(path, ours_exe):
    cmds = []
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0] == "set_range" and not NUM.match(parts[2].split()[0]):
                line = f"set_range {parts[1]} {range_to_floats(ours_exe, parts[2])}"
            cmds.append(line)
    return ["set_end_string END"] + cmds + ["exit"]


def run_ours(exe, cmds, timeout):
    p = subprocess.run([exe], input="\n".join(cmds) + "\n", capture_output=True, text=True, timeout=timeout)
    return p.stdout


def run_pio(cmds, timeout):
    exe = os.environ.get("PIO_EXE", r"C:\PioSOLVER\PioSOLVER3-edge-znver5.exe")
    folder, name = exe.rsplit("\\", 1)
    if os.environ.get("PIO_LOCAL") == "1":  # running on the Pio machine itself
        p = subprocess.run([exe], cwd=folder, input="\r\n".join(cmds) + "\r\n", capture_output=True, text=True,
                           timeout=timeout)
        return p.stdout.replace("\r", "")
    dest = os.environ["PIO_SSH"]  # e.g. user@windows-host (required)
    key = os.path.expanduser(os.environ.get("PIO_KEY", "~/.ssh/pio_pc_ed25519"))
    remote = f'cd /d {folder} && {name}'
    p = subprocess.run(["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", dest, remote],
                       input="\r\n".join(cmds) + "\r\n", capture_output=True, text=True, timeout=timeout)
    return p.stdout.replace("\r", "")


def split_responses(text):
    """Return (responses, solver_msgs). Startup banner (before the first response) is dropped."""
    lines = text.split("\n")
    responses, solver, cur = [], [], []
    i = 0
    started = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("[LOG]"):
            solver.append([line])
            i += 1
            continue
        if line.startswith("SOLVER:") and not cur:
            if line.strip() == "SOLVER:":
                block = []
                i += 1
                while i < len(lines) and lines[i] != END:
                    block.append(lines[i])
                    i += 1
                solver.append(["SOLVER:"] + block)
            else:
                solver.append([line])
            i += 1
            continue
        if line == END:
            if started:
                responses.append("\n".join(cur))
            else:
                started = True  # response to set_end_string; banner lines are in cur
                responses.append(cur[-1] if cur else "")
            cur = []
        else:
            cur.append(line)
        i += 1
    return responses, solver


def numeric_close(a, b, tol):
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb) or not ta:
        return None
    worst = 0.0
    for x, y in zip(ta, tb):
        if NUM.match(x) and NUM.match(y):
            fx, fy = float(x), float(y)
            worst = max(worst, abs(fx - fy) / max(1.0, abs(fx), abs(fy)))
        elif x != y:
            return None
    return worst if worst <= tol else -worst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--out", default=None)
    ap.add_argument("--ours", default="./solver")
    ap.add_argument("--tol", type=float, default=1e-3, help="relative tolerance for numeric outputs")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--show", type=int, default=6, help="lines of each side to show per difference")
    ap.add_argument("--exact", action="store_true", help="report formatting differences too (byte comparison)")
    args = ap.parse_args()
    cmds = script_commands(args.script, args.ours)
    out = args.out or os.path.splitext(args.script)[0] + "_results"
    os.makedirs(out, exist_ok=True)
    pio_raw = run_pio(cmds, args.timeout)
    ours_raw = run_ours(args.ours, cmds, args.timeout)
    open(os.path.join(out, "pio.txt"), "w").write(pio_raw)
    open(os.path.join(out, "ours.txt"), "w").write(ours_raw)
    pr, ps = split_responses(pio_raw)
    orr, osv = split_responses(ours_raw)
    report = []
    same = differ = 0
    for i, cmd in enumerate(cmds[:-1]):
        a = pr[i] if i < len(pr) else "<missing>"
        b = orr[i] if i < len(orr) else "<missing>"
        if a == b:
            same += 1
            continue
        nc = numeric_close(a, b, args.tol)
        if nc is not None and nc >= 0:
            if not args.exact:
                same += 1
                continue
            differ += 1
            al, bl = a.split("\n"), b.split("\n")
            k = next((k for k in range(min(len(al), len(bl))) if al[k] != bl[k]), min(len(al), len(bl)))
            ta = repr(al[k][-40:]) if k < len(al) else "<no line>"
            tb = repr(bl[k][-40:]) if k < len(bl) else "<no line>"
            report.append(f"=== [{i}] {cmd[:100]}  -- within tolerance, bytes differ: {len(al)} vs {len(bl)} lines, "
                          f"first differing line {k}: pio ...{ta}  ours ...{tb}")
            continue
        differ += 1
        tag = f"numeric (max rel diff {-nc:.3g})" if nc is not None else "text"
        report.append(f"=== [{i}] {cmd[:100]}  -- {tag}")
        al, bl = a.split("\n"), b.split("\n")
        report.append("  pio : " + "\n        ".join(l[:160] for l in al[: args.show]) + ("  ..." if len(al) > args.show else ""))
        report.append("  ours: " + "\n        ".join(l[:160] for l in bl[: args.show]) + ("  ..." if len(bl) > args.show else ""))
    report.append(f"\n{same} identical/within tolerance, {differ} different; "
                  f"responses pio={len(pr)} ours={len(orr)} commands={len(cmds) - 1}")
    report.append(f"SOLVER messages pio={len(ps)} ours={len(osv)}")
    if ps:
        report.append("pio SOLVER sample: " + " | ".join(ps[-1]))
    if osv:
        report.append("ours SOLVER sample: " + " | ".join(osv[-1]))
    text = "\n".join(report)
    open(os.path.join(out, "report.txt"), "w").write(text)
    print(text)


if __name__ == "__main__":
    main()
