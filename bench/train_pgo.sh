#!/bin/sh
# Training workload for the profile-guided optimization in `make release`.
#
# Runs the instrumented solver over the benchmark trees so the collected profile
# covers the hot paths: tree building, vector DCFR iterations (single- and
# multi-threaded), chance-node expansion, terminal evaluation, best response
# (calc_results), and node queries. Replies are discarded — only which code ran
# matters, so solves use fixed step counts to keep the instrumented run bounded.
#
# Usage: bench/train_pgo.sh <instrumented-solver> <profile-dir> [llvm-profdata]
#   $1  path to the instrumented solver binary
#   $2  profile directory (clang: raw *.profraw accumulate here, merged into
#       merged.profdata; gcc: `make` passes -fprofile-generate=<dir>/gcc and the
#       .gcda files accumulate on their own)
#   $3  llvm-profdata binary; empty for GCC, which needs no merge step.
set -eu

EXE=$1
PROFDIR=$2
PROFDATA_TOOL=${3:-}

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
mkdir -p "$PROFDIR/gcc"   # only used by the GCC build

NCPU=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)

# Strip comments and blank lines from a bench tree file, like bench/run.py does.
setup() {
    grep -v '^#' "$1" | grep -v '^[[:space:]]*$'
}

# 1. The speed-benchmark script as-is: mid-size flop tree, 1 thread, 60 steps.
"$EXE" < tests/pio_compare/bench_flop.txt > /dev/null

# 2. River tree on all cores: smallest tree in the suite — thousands of cheap
#    iterations exercise terminal/showdown evaluation and the thread pool.
{
    setup bench/trees/river.upi
    echo "set_threads $NCPU"
    echo "build_tree"
    echo "go 2000 steps"
    echo "wait_for_solver"
    echo "calc_results"
    echo "show_strategy r:0"
    echo "show_children r:0"
} | "$EXE" > /dev/null

# 3. Turn tree, single-threaded and all-core: a chance node plus two decision
#    streets, so chance expansion and isomorphism handling get profiled too.
for T in 1 "$NCPU"; do
    {
        setup bench/trees/turn.upi
        echo "set_threads $T"
        echo "build_tree"
        echo "go 400 steps"
        echo "wait_for_solver"
        echo "calc_results"
    } | "$EXE" > /dev/null
done

# Clang: merge the per-process raw profiles. GCC needs nothing here: repeated
# instrumented runs accumulate into the same .gcda files.
if [ -n "$PROFDATA_TOOL" ]; then
    "$PROFDATA_TOOL" merge -output="$PROFDIR/merged.profdata" "$PROFDIR"/*.profraw
fi
