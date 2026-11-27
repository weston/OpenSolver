#!/usr/bin/env python3
"""Benchmark harness: wall time to reach a target exploitability on each tree.

The metric is time-to-accuracy, not iterations per second: a faster iteration that
converges more slowly is not an improvement. Each tree is solved once per thread mode with
the accuracy target set to the tightest value, and the periodic SOLVER blocks are parsed to
find when every target was first crossed. One run therefore yields all three targets, which
is both faster and more consistent than re-solving per target.

Usage:
    python3 bench/run.py                        # all trees, 1 thread and all threads
    python3 bench/run.py --trees river,turn     # a subset
    python3 bench/run.py --threads 1            # one mode only
    python3 bench/run.py --targets 0.01,0.002   # fractions of the starting pot
    python3 bench/run.py --max-seconds 120      # give up on a tree after this long
    python3 bench/run.py --include-preflop      # + full preflop tree (GBs, minutes)
    python3 bench/run.py --include-big          # + realistic 13 GB SRP tree (needs ~14 GB RAM)
    python3 bench/run.py --out bench/results    # write results.json and results.md
    make bench

Targets are fractions of the starting pot, matching `set_accuracy <x> fraction`.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TREES = os.path.join(HERE, "trees")

# The 25bb fixture is a real PioViewer-generated script that already carries build_tree;
# it is reused rather than duplicated, and is heavy enough to be opt-in.
FIXTURE_25BB = os.path.join(ROOT, "tests", "data", "pio_25bbHU_2sizes.txt")

DEFAULT_TARGETS = (0.01, 0.005, 0.002)
# Exploitability is only tested when the solver reports, so this bounds how far past a
# target a run can go. Small enough to be tight, large enough that the best-response work
# does not dominate the measurement.
DEFAULT_INFO_FREQ = 5


# Trees that are too heavy for the default suite; added only via their --include-* flag.
# preflop_full is a real file under trees/ but needs several GB and minutes per run.
HEAVY_TREES = {"preflop_full", "srp_big"}
HUNL_PRESETS = {
    "hunl_srp": "HUNL SRP",
    "hunl_srp_mono": "HUNL SRP",
    "hunl_3bp": "HUNL 3BP",
    "hunl_3bp_extra": "HUNL 3BP",
}


def preset_setup(name: str) -> list[str]:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from viewer.server import setup_from_body

    with open(os.path.join(ROOT, "viewer", "defaults", "configs", HUNL_PRESETS[name] + ".json")) as fh:
        config = json.load(fh)
    if name == "hunl_srp_mono":
        config["board"] = "AsQs5s"
    if name == "hunl_3bp_extra":
        for key in ("flop_bet", "flop_bet_ip"):
            config[key] += ",50"
    return setup_from_body(config)[0]


def tree_names() -> list[str]:
    return sorted(f[:-4] for f in os.listdir(TREES)
                  if f.endswith(".upi") and f[:-4] not in HEAVY_TREES)


def peak_rss_sampler(proc: subprocess.Popen, out: dict, interval: float = 0.05) -> None:
    """Poll the child's RSS and keep the maximum.

    getrusage(RUSAGE_CHILDREN) only gives a high-water mark across every child the process
    has ever reaped, so it cannot attribute a peak to one run in a sequence. Sampling `ps`
    is portable across macOS and Linux and does attribute it. A run shorter than a couple
    of samples can under-report, which is why the fastest trees are reported with a note.
    """
    peak = 0
    while proc.poll() is None:
        try:
            r = subprocess.run(["ps", "-o", "rss=", "-p", str(proc.pid)],
                               capture_output=True, text=True, timeout=5)
            v = r.stdout.strip()
            if v:
                peak = max(peak, int(v.split()[0]) * 1024)  # ps reports KiB on both OSes
        except Exception:
            pass
        time.sleep(interval)
    out["peak_rss"] = peak


def build_script(name: str, threads: int, target: float, info_freq: int | str,
                 max_seconds: int, extra: tuple[str, ...] = (),
                 pre: tuple[str, ...] = ()) -> str:
    """Tree definition plus the commands that drive one measured solve."""
    if name == "fixture25bb":
        with open(FIXTURE_25BB) as fh:
            # The fixture ends in build_tree and carries its own ranges/board/lines.
            setup = [l.rstrip() for l in fh if l.strip() and not l.startswith("#")]
        # set_threads and any --pre-build settings have to precede build_tree, which
        # the fixture already contains.
        idx = next(i for i, l in enumerate(setup) if l.startswith("build_tree"))
        ins = []
        # Keep peak memory in reach of a normal machine, unless --pre-build overrides.
        if not any(l.startswith("small_strats") for l in pre):
            ins.append("small_strats on")
        ins.append(f"set_threads {threads}")
        ins.extend(pre)
        setup[idx:idx] = ins
        body = setup
    else:
        if name in HUNL_PRESETS:
            setup = preset_setup(name)
        else:
            with open(os.path.join(TREES, f"{name}.upi")) as fh:
                setup = [l.rstrip() for l in fh if l.strip() and not l.startswith("#")]
        # A file may carry its own build command when commands must follow it (e.g.
        # preflop_full attaches schematic trees after build_preflop_tree). set_threads
        # and --pre-build settings still have to come first, so they are inserted
        # ahead of that line.
        carried = next((i for i, l in enumerate(setup)
                        if l.startswith(("build_preflop_tree", "build_tree"))), None)
        if carried is not None:
            setup[carried:carried] = [f"set_threads {threads}"] + list(pre)
            body = setup
        else:
            preflop = any(l.startswith("add_preflop_line") for l in setup)
            body = (setup + [f"set_threads {threads}"] + list(pre) +
                    ["build_preflop_tree" if preflop else "build_tree"])

    # extra commands land after the build and before the solve, which is where settings
    # like set_dcfr have to be set.
    return "\n".join(body + list(extra) + [
        f"set_accuracy {target} fraction",
        f"set_info_freq {info_freq}",
        f"go {max_seconds} seconds" if max_seconds else "go",
        "wait_for_solver",
        "calc_results",
        "show_iters",
        "solver_time",
        "exit",
    ]) + "\n"


BLOCK_TIME = re.compile(r"^running time:\s*([0-9.]+)", re.M)
BLOCK_EXPL = re.compile(r"^Exploitable for:\s*([0-9.eE+-]+)", re.M)


def parse_progress(out: str) -> list[tuple[float, float]]:
    """(elapsed seconds, exploitability) from each periodic SOLVER block, in order."""
    times = [float(m.group(1)) for m in BLOCK_TIME.finditer(out)]
    expls = [float(m.group(1)) for m in BLOCK_EXPL.finditer(out)]
    return list(zip(times, expls))


def run_one(exe: str, name: str, threads: int, targets: tuple[float, ...],
            info_freq: int | str, max_seconds: int, extra: tuple[str, ...] = (),
            pre: tuple[str, ...] = ()) -> dict:
    script = build_script(name, threads, min(targets), info_freq, max_seconds, extra, pre)
    proc = subprocess.Popen([exe], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, cwd=ROOT)
    rss: dict = {}
    sampler = threading.Thread(target=peak_rss_sampler, args=(proc, rss), daemon=True)
    sampler.start()
    t0 = time.monotonic()
    out, _ = proc.communicate(script)
    wall = time.monotonic() - t0
    sampler.join(timeout=2)

    progress = parse_progress(out)
    # Final exploitability: calc_results prints one last block after the solve.
    final = progress[-1][1] if progress else float("nan")
    # show_iters is the only command here whose whole response is a bare integer
    # (solver_time prints a float, node_count prefixes "TOTAL:").
    m = re.findall(r"^(\d+)$", out, re.M)
    iters = int(m[-1]) if m else 0

    # First report at or below each target.
    pot_line = next((line for line in script.splitlines() if line.startswith("set_pot ")), "")
    pot = sum(float(x) for x in pot_line.split()[1:4]) or 1.0
    reached: dict[str, float | None] = {}
    for t in sorted(targets, reverse=True):
        hit = next((sec for sec, e in progress if e <= t * pot), None)
        reached[f"{t}"] = hit

    return {
        "tree": name,
        "threads": threads,
        "wall_seconds": round(wall, 3),
        "iterations": iters,
        "device_reports": re.findall(r"^SOLVER: device (.+)$", out, re.M),
        "final_exploitability": final,
        "peak_rss_mb": round(rss.get("peak_rss", 0) / 2**20, 1),
        "reached": reached,
        "extra": list(extra),
        "pre": list(pre),
        "error": next((l for l in out.splitlines() if l.startswith("ERROR")), None),
    }


def markdown(rows: list[dict], targets: tuple[float, ...], meta: dict) -> str:
    head = ["tree", "threads", "iters", "peak RSS"]
    head += [f"to {t * 100:g}% pot" for t in sorted(targets, reverse=True)]
    head += ["final expl", "Pio (same tree)"]
    out = [f"# Benchmark results\n",
           f"- machine: {meta['machine']}",
           f"- compiler: {meta['compiler']}",
           f"- solver: {meta['version']}",
           f"- targets are exploitability as a fraction of the starting pot",
           f"- times are seconds to the first report at or below the target; "
           f"`set_info_freq {meta['info_freq']}` bounds the overshoot",
           f"- the Pio column is for the maintainer to fill from a licensed PioSOLVER on the "
           f"same trees (black-box, via `tests/pio_compare`)",
           f"- \"not reached\" means the run hit the `--max-seconds {meta.get('max_seconds', '')}` "
           f"wall-clock cap before the target, not that the solve failed or stalled; raise the cap "
           f"to measure that row\n",
           "| " + " | ".join(head) + " |",
           "|" + "---|" * len(head)]
    for r in rows:
        cells = [r["tree"], str(r["threads"]), str(r["iterations"]), f"{r['peak_rss_mb']:g} MB"]
        for t in sorted(targets, reverse=True):
            v = r["reached"].get(f"{t}")
            cells.append(f"{v:.2f}" if v is not None else "not reached")
        cells.append(f"{r['final_exploitability']:.4f}")
        cells.append("")
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", default=os.path.join(ROOT, "solver"))
    ap.add_argument("--trees", default="", help="comma-separated trees, including " + ", ".join(HUNL_PRESETS))
    ap.add_argument("--threads", default="", help="comma-separated modes (default: 1 and all)")
    ap.add_argument("--targets", default=",".join(str(t) for t in DEFAULT_TARGETS))
    ap.add_argument("--info-freq", type=lambda s: s if s == "auto" else int(s), default=DEFAULT_INFO_FREQ)
    ap.add_argument("--max-seconds", type=int, default=300,
                    help="per-run cap; 0 means no cap (default 300)")
    ap.add_argument("--include-25bb", action="store_true",
                    help="also run the 25bb fixture (several GB, minutes per run)")
    ap.add_argument("--include-preflop", action="store_true",
                    help="also run the full preflop tree (several GB, minutes per run)")
    ap.add_argument("--include-big", action="store_true",
                    help="also run srp_big, a realistic 100bb SRP tree (12.9 GB float32)")
    ap.add_argument("--out", default="", help="write <out>.json and <out>.md")
    ap.add_argument("--extra", default="",
                    help="semicolon-separated UPI commands inserted after the build and "
                         "before the solve, e.g. --extra 'set_dcfr 1.5 0 3'")
    ap.add_argument("--pre-build", default="",
                    help="semicolon-separated UPI commands inserted before the build "
                         "command, e.g. --pre-build 'small_strats on' (overrides the "
                         "25bb fixture's built-in small_strats on)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.exe):
        print(f"no solver at {args.exe}; run make first", file=sys.stderr)
        return 1

    names = [t for t in args.trees.split(",") if t] or tree_names()
    if args.include_25bb:
        names = names + ["fixture25bb"]
    if args.include_preflop:
        names = names + ["preflop_full"]
    if args.include_big:
        names = names + ["srp_big"]
    ncpu = os.cpu_count() or 1
    modes = [int(t) for t in args.threads.split(",") if t] or sorted({1, ncpu})
    targets = tuple(float(t) for t in args.targets.split(",") if t)

    meta = {
        "machine": f"{platform.machine()} {platform.system()} {platform.release()}, {ncpu} cores",
        "compiler": subprocess.run(["c++", "--version"], capture_output=True,
                                   text=True).stdout.splitlines()[0],
        "version": subprocess.run([args.exe], input="show_build_version\nexit\n",
                                  capture_output=True, text=True).stdout.strip().splitlines()[-1],
        "info_freq": args.info_freq,
        "max_seconds": args.max_seconds,
        "extra": args.extra,
        "pre_build": args.pre_build,
    }

    rows = []
    for name in names:
        for threads in modes:
            print(f"  {name} @ {threads} thread{'s' if threads > 1 else ''} ...",
                  end="", flush=True)
            r = run_one(args.exe, name, threads, targets, args.info_freq, args.max_seconds,
                        tuple(c.strip() for c in args.extra.split(";") if c.strip()),
                        tuple(c.strip() for c in args.pre_build.split(";") if c.strip()))
            rows.append(r)
            if r["error"]:
                print(f" {r['error']}")
            else:
                best = r["reached"].get(f"{min(targets)}")
                print(f" {r['wall_seconds']}s wall, {r['iterations']} iters, "
                      f"tightest target {'%.2fs' % best if best else 'not reached'}")

    table = markdown(rows, targets, meta)
    if args.out:
        with open(args.out + ".json", "w") as fh:
            json.dump({"meta": meta, "runs": rows}, fh, indent=2)
        with open(args.out + ".md", "w") as fh:
            fh.write(table)
        print(f"\nwrote {args.out}.json and {args.out}.md")
    else:
        print()
        print(table)
    return 1 if any(r["error"] for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
