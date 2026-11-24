# OpenUPI-Solver build. `make` builds ./solver; `make test` runs the unit and integration tests.
# `make test-big` also runs the 25bb fixture section of test_upi.py (~4 GB of free RAM needed).
# macOS's JavaScriptCore shell: runs the viewer's JS unit tests when Node is not installed
JSC ?= /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc
CXX      ?= clang++
# ARM compilers use -mcpu; x86 compilers use -march. Inspect the compiler's
# target so this also works when building for a target other than the host.
TARGET_ARCH := $(firstword $(subst -, ,$(shell $(CXX) -dumpmachine)))
ifneq ($(filter aarch64 arm64 arm%,$(TARGET_ARCH)),)
NATIVE_ARCH := -mcpu=native
else
NATIVE_ARCH := -march=native
endif
CXXFLAGS ?= -std=c++20 -O3 $(NATIVE_ARCH) -Wall -Wextra -DNDEBUG
LDLIBS   := -lz -lpthread
SRC      := src/cards.cpp src/eval.cpp src/range.cpp src/action_tree.cpp src/game.cpp src/solver.cpp \
            src/parallel.cpp src/equity.cpp src/categories.cpp src/pfh2h.cpp src/upi.cpp src/upi_nodes.cpp src/upi_save.cpp
OBJ      := $(SRC:src/%.cpp=build/%.o)

all: solver

build/%.o: src/%.cpp $(wildcard src/*.hpp) | build
	$(CXX) $(CXXFLAGS) -c $< -o $@

build:
	mkdir -p build

solver: $(OBJ) build/main.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

LIBOBJ := $(filter-out build/upi.o build/upi_nodes.o build/upi_save.o,$(OBJ))

build/test_%: tests/test_%.cpp $(LIBOBJ)
	$(CXX) $(CXXFLAGS) $(filter %.cpp %.o,$^) -o $@ $(LDLIBS)

# Flop subsets for preflop trees (#486): the generator of flops/wsolver_<N>.txt. `make flop-subsets`
# rewrites those files (about a minute of every core the first time; see tools/flop_subsets.cpp).
FLOP_SIZES ?= 10 25 50 100 200
build/flop_subsets: tools/flop_subsets.cpp tools/flop_subsets.hpp $(LIBOBJ)
	$(CXX) $(CXXFLAGS) $< $(LIBOBJ) -o $@ $(LDLIBS)
build/test_flop_subsets: tools/flop_subsets.hpp
flop-subsets: build/flop_subsets
	./build/flop_subsets make $(FLOP_SIZES) --dir flops

test: solver build/test_eval build/test_range build/test_categories build/test_solver build/test_pfh2h build/test_parallel build/test_flop_subsets build/flop_subsets
	./build/test_eval
	./build/test_range
	./build/test_categories
	./build/test_solver
	./build/test_pfh2h
	./build/test_parallel
	./build/test_flop_subsets
	python3 tests/test_tree_builder.py
	python3 tests/test_packaging.py
	python3 tests/test_jobs.py ./solver
	python3 tests/test_preflop.py ./solver
	python3 tests/test_upi.py ./solver
	python3 viewer/test_server.py ./solver
	python3 viewer/test_library.py
	python3 viewer/test_resolve.py ./solver
	python3 viewer/test_locks.py ./solver
	python3 viewer/test_nl_lock.py ./solver
	if command -v node >/dev/null; then node viewer/test_lock_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_lock_js.mjs; else echo "node not found: skipping viewer/test_lock_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_sizes_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_sizes_js.mjs; else echo "node not found: skipping viewer/test_sizes_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_blockers_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_blockers_js.mjs; else echo "node not found: skipping viewer/test_blockers_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_graphs_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_graphs_js.mjs; else echo "node not found: skipping viewer/test_graphs_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_sidecache_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_sidecache_js.mjs; else echo "node not found: skipping viewer/test_sidecache_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_runouts_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_runouts_js.mjs; else echo "node not found: skipping viewer/test_runouts_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_preflop_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_preflop_js.mjs; else echo "node not found: skipping viewer/test_preflop_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_build_page_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_build_page_js.mjs; else echo "node not found: skipping viewer/test_build_page_js.mjs"; fi
	python3 viewer/test_hand_lines.py ./solver
	python3 viewer/test_preflop_view.py ./solver
	python3 viewer/test_preflop_build.py ./solver
	python3 viewer/test_jobs_api.py ./solver
	python3 viewer/test_refine.py ./solver

test-big: solver build/test_eval build/test_range build/test_categories build/test_solver build/test_pfh2h build/test_parallel build/test_flop_subsets build/flop_subsets
	./build/test_eval
	./build/test_range
	./build/test_categories
	./build/test_solver
	./build/test_pfh2h
	./build/test_parallel
	./build/test_flop_subsets
	python3 tests/test_tree_builder.py
	python3 tests/test_packaging.py
	python3 tests/test_jobs.py ./solver
	python3 tests/test_preflop.py ./solver
	python3 tests/test_upi.py ./solver --big
	python3 viewer/test_server.py ./solver
	python3 viewer/test_library.py
	python3 viewer/test_resolve.py ./solver
	python3 viewer/test_locks.py ./solver
	python3 viewer/test_nl_lock.py ./solver
	if command -v node >/dev/null; then node viewer/test_lock_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_lock_js.mjs; else echo "node not found: skipping viewer/test_lock_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_sizes_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_sizes_js.mjs; else echo "node not found: skipping viewer/test_sizes_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_blockers_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_blockers_js.mjs; else echo "node not found: skipping viewer/test_blockers_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_graphs_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_graphs_js.mjs; else echo "node not found: skipping viewer/test_graphs_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_sidecache_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_sidecache_js.mjs; else echo "node not found: skipping viewer/test_sidecache_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_runouts_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_runouts_js.mjs; else echo "node not found: skipping viewer/test_runouts_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_preflop_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_preflop_js.mjs; else echo "node not found: skipping viewer/test_preflop_js.mjs"; fi
	if command -v node >/dev/null; then node viewer/test_build_page_js.mjs; elif [ -x $(JSC) ]; then $(JSC) viewer/jsc_prelude.js -m viewer/test_build_page_js.mjs; else echo "node not found: skipping viewer/test_build_page_js.mjs"; fi
	python3 viewer/test_hand_lines.py ./solver
	python3 viewer/test_preflop_view.py ./solver
	python3 viewer/test_preflop_build.py ./solver
	python3 viewer/test_jobs_api.py ./solver
	python3 viewer/test_refine.py ./solver

# Cross-compile a static Windows binary (solver.exe) from macOS/Linux with mingw-w64. ZLIB_DIR must
# contain zlib.h and a MinGW libz.a (e.g. zlib's own: make -f win32/Makefile.gcc PREFIX=x86_64-w64-mingw32-).
WIN_CXX  ?= x86_64-w64-mingw32-g++
WIN_ARCH ?= -march=x86-64-v3
ZLIB_DIR ?=
WIN_OBJ  := $(SRC:src/%.cpp=build/win/%.o) build/win/main.o

build/win/%.o: src/%.cpp $(wildcard src/*.hpp)
	@mkdir -p build/win
	$(WIN_CXX) -std=c++20 -O3 $(WIN_ARCH) -Wall -Wextra -DNDEBUG $(if $(ZLIB_DIR),-I$(ZLIB_DIR)) -c $< -o $@

windows: $(WIN_OBJ)
	$(WIN_CXX) -std=c++20 -O3 $(WIN_ARCH) -static $^ -o build/win/solver.exe $(if $(ZLIB_DIR),-L$(ZLIB_DIR)) -lz

bench: solver
	python3 bench/run.py --out bench/results

# ----- release build: LTO, and optionally PGO ---------------------------------
# `make release` produces ./solver-release without touching the default ./solver
# or the default build flags. By default it compiles with LTO and the arch flags
# below. With PGO=1 it additionally:
#   1. compiles an instrumented binary into build/pgo/gen/,
#   2. runs bench/train_pgo.sh (tests/pio_compare/bench_flop.txt plus the bench
#      river/turn trees, 1 thread and all threads) to collect a profile,
#   3. compiles the final binary with that profile as well.
# RELEASE_ARCH defaults to native on both platforms (ARM: -mcpu, x86: -march),
# like the default build. Override it for portable binaries, e.g. on Linux:
#   make release RELEASE_ARCH=-march=x86-64-v3
# and on Apple Silicon (M1 and later):
#   make release RELEASE_ARCH=-mcpu=apple-m1
RELEASE_ARCH     ?= $(NATIVE_ARCH)
RELEASE_CXXFLAGS ?= -std=c++20 -O3 $(RELEASE_ARCH) -Wall -Wextra -DNDEBUG

PGO_DIR  := build/pgo
GEN_OBJ  := $(SRC:src/%.cpp=$(PGO_DIR)/gen/%.o) $(PGO_DIR)/gen/main.o
OPT_OBJ  := $(SRC:src/%.cpp=$(PGO_DIR)/opt/%.o) $(PGO_DIR)/opt/main.o
PGO_DEPS := $(wildcard src/*.hpp)

CXX_IS_CLANG := $(shell $(CXX) --version 2>/dev/null | head -n1 | grep -qi clang && echo 1)
ifeq ($(CXX_IS_CLANG),1)
# Xcode does not put llvm-profdata on PATH; xcrun finds it when it is there.
LLVM_PROFDATA ?= $(shell command -v llvm-profdata 2>/dev/null || xcrun -f llvm-profdata 2>/dev/null)
# IR-level instrumentation (-fprofile-generate), NOT front-end (-fprofile-instr-generate).
# Front-end instrumentation counts function entries before inlining, so a function that the
# instrumented build inlines records entry count 0. Clang then reads 0 at optimise time, calls the
# function cold, drops its inline threshold to 45, and refuses to inline it. That hit the hottest
# path we have: showdown_batch (cost 155) stopped being inlined into Solver::river_cfr, and the
# profile claimed showdown_batch and river_cfr were entered 0 times while the kernel they call,
# showdown_board_scalar, was entered 2,646,970 times. Net effect was a binary 11.7% SLOWER than
# using no profile at all. IR-level instrumentation counts after inlining and does not invert like
# this. See #199; don't switch back without re-measuring.
PGO_GEN  := -fprofile-generate=$(CURDIR)/$(PGO_DIR)
PGO_USE  := -fprofile-use=$(CURDIR)/$(PGO_DIR)/merged.profdata
LTO_FLAG := -flto=thin
else
PGO_GEN  := -fprofile-generate=$(CURDIR)/$(PGO_DIR)/gcc
PGO_USE  := -fprofile-use=$(CURDIR)/$(PGO_DIR)/gcc -fprofile-correction
LTO_FLAG := -flto=auto
endif

# PGO is opt-in (#199). Measured on arm64 (M1 Ultra, flop_srp_2, 60 steps,
# 1 thread, interleaved with alternating order, 8 reps), relative to the default
# `make` build:
#     LTO only       -0.2%
#     LTO + PGO      -0.1%
# PGO is at parity, not a win -- an isolated 12-rep run put it at -1.65%, but
# that is only 1.2x sd and does not clear the 2x bar this repo uses. Since it
# roughly doubles release build time for no measured gain, it stays off by
# default. It is worth re-testing per machine and workload.
# (Before the IR-instrumentation fix, PGO was +12.1% SLOWER here; see the
# PGO_GEN comment above for why, and do not switch those flags back.)
# To build with PGO:  make release PGO=1
PGO ?= 0
ifeq ($(PGO),1)
PGO_TRAIN_DEP := $(PGO_DIR)/trained.stamp
else
PGO_USE       :=
PGO_TRAIN_DEP :=
endif

release: solver-release

$(PGO_DIR)/gen $(PGO_DIR)/opt:
	mkdir -p $@

$(PGO_DIR)/gen/%.o: src/%.cpp $(PGO_DEPS) | $(PGO_DIR)/gen
	$(CXX) $(RELEASE_CXXFLAGS) $(PGO_GEN) -c $< -o $@

$(PGO_DIR)/instrumented: $(GEN_OBJ)
	$(CXX) $(RELEASE_CXXFLAGS) $(PGO_GEN) $^ -o $@ $(LDLIBS)

$(PGO_DIR)/trained.stamp: $(PGO_DIR)/instrumented bench/train_pgo.sh
	LLVM_PROFILE_FILE=$(CURDIR)/$(PGO_DIR)/%p.profraw \
	    bench/train_pgo.sh $(PGO_DIR)/instrumented $(CURDIR)/$(PGO_DIR) "$(LLVM_PROFDATA)"
	touch $@

$(PGO_DIR)/opt/%.o: src/%.cpp $(PGO_DEPS) $(PGO_TRAIN_DEP) | $(PGO_DIR)/opt
	$(CXX) $(RELEASE_CXXFLAGS) $(LTO_FLAG) $(PGO_USE) -c $< -o $@

solver-release: $(OPT_OBJ)
	$(CXX) $(RELEASE_CXXFLAGS) $(LTO_FLAG) $(PGO_USE) $^ -o $@ $(LDLIBS)

# ----- install ----------------------------------------------------------------
# Standard PREFIX/DESTDIR install, so there is a supported way to put the solver
# on a machine instead of running it out of the build tree.
#   make install                      -> /usr/local/bin/wsolver
#   make install PREFIX=$HOME/.local  -> ~/.local/bin/wsolver
#   make install DESTDIR=/tmp/stage   -> staged, for packaging
# INSTALL_BIN picks which binary to install; point it at solver-release to ship
# the LTO build (see the release section above):
#   make release && make install INSTALL_BIN=solver-release
# BINNAME is the installed command name. It is deliberately not "solver", which
# is too generic for a shared bin directory.
PREFIX      ?= /usr/local
DESTDIR     ?=
BINNAME     ?= wsolver
INSTALL_BIN ?= solver
DOCDIR      ?= $(PREFIX)/share/doc/$(BINNAME)

install: $(INSTALL_BIN)
	install -d $(DESTDIR)$(PREFIX)/bin $(DESTDIR)$(DOCDIR)
	install -m 755 $(INSTALL_BIN) $(DESTDIR)$(PREFIX)/bin/$(BINNAME)
	install -m 644 README.md docs/commands.md $(DESTDIR)$(DOCDIR)/

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/$(BINNAME)
	rm -rf $(DESTDIR)$(DOCDIR)

# ----- source distribution -----------------------------------------------------
# `make dist` writes dist/wsolver-<version>-src.tar.gz plus a .sha256 next to it,
# so a recipient can verify what they got. VERSION is read from src/version.hpp
# rather than duplicated here, and `git archive` means the tarball contains
# exactly what is committed -- no build output, no stray local files.
# NB: no '#' in this sed script -- make strips from '#' to end of line even
# inside $(shell ...), which silently truncates the call.
VERSION  := $(shell sed -n 's/.*SOLVER_VERSION "\(.*\)".*/\1/p' src/version.hpp)
DISTNAME := $(BINNAME)-$(VERSION)-src

dist:
	@test -n "$(VERSION)" || { echo "could not read SOLVER_VERSION from src/version.hpp"; exit 1; }
	@git diff-index --quiet HEAD -- || echo "WARNING: uncommitted changes to tracked files are NOT in the tarball"
	@u=$$(git ls-files --others --exclude-standard | head -5); \
	  test -z "$$u" || { echo "WARNING: untracked files are NOT in the tarball:"; echo "$$u" | sed 's/^/  /'; }
	mkdir -p dist
	git archive --format=tar.gz --prefix=$(DISTNAME)/ -o dist/$(DISTNAME).tar.gz HEAD
	cd dist && shasum -a 256 $(DISTNAME).tar.gz > $(DISTNAME).tar.gz.sha256
	@echo "dist/$(DISTNAME).tar.gz"
	@cat dist/$(DISTNAME).tar.gz.sha256

# ----- macOS installer (#479) --------------------------------------------------
# `make macos-pkg` writes dist/WSolver-<version>-macos-arm64.pkg (and its .sha256):
# an installer for /Applications/WSolver.app, which holds the solver, the viewer
# and a Python to run it, and starts the viewer when opened. See README, "macOS
# installer", and packaging/macos/build.sh.
# The solver in it is a build of its own: for any Apple Silicon Mac rather than
# this one (-mcpu=apple-m1, where the default build is -mcpu=native), with LTO
# as in `make release`, in its own object directory so that neither build is
# linked from the other's objects.
# MACOS_MIN is the oldest macOS the app runs on. 13.3 is the first whose C++
# library has the floating-point std::to_chars that src/upi.cpp prints with.
MACOS_MIN      ?= 13.3
MACOS_CXXFLAGS := -std=c++20 -O3 -mcpu=apple-m1 -arch arm64 -mmacosx-version-min=$(MACOS_MIN) $(LTO_FLAG) -Wall -Wextra -DNDEBUG
MACOS_OBJ      := $(SRC:src/%.cpp=build/macos/obj/%.o) build/macos/obj/main.o

build/macos/obj:
	mkdir -p $@

build/macos/obj/%.o: src/%.cpp $(wildcard src/*.hpp) | build/macos/obj
	$(CXX) $(MACOS_CXXFLAGS) -c $< -o $@

build/macos/solver: $(MACOS_OBJ)
	$(CXX) $(MACOS_CXXFLAGS) $^ -o $@ $(LDLIBS)

ifeq ($(shell uname -sm),Darwin arm64)
macos-pkg: build/macos/solver
	@test -n "$(VERSION)" || { echo "could not read SOLVER_VERSION from src/version.hpp"; exit 1; }
	VERSION=$(VERSION) MACOS_MIN=$(MACOS_MIN) SOLVER=build/macos/solver packaging/macos/build.sh
else
macos-pkg:
	@echo "the macOS package is built on an Apple Silicon Mac"; exit 1
endif

clean:
	rm -rf build solver solver-release dist

.PHONY: all test test-big flop-subsets windows bench release install uninstall dist macos-pkg clean
