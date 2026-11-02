# OpenUPI-Solver build. `make` builds ./solver; `make test` runs the unit and integration tests.
# `make test-big` also runs the 25bb fixture section of test_upi.py (~4 GB of free RAM needed).
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
            src/parallel.cpp src/equity.cpp src/categories.cpp src/pfh2h.cpp src/upi.cpp src/upi_nodes.cpp src/upi_save.cpp src/nn_warm.cpp
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
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

test: solver build/test_eval build/test_range build/test_categories build/test_solver build/test_pfh2h
	./build/test_eval
	./build/test_range
	./build/test_categories
	./build/test_solver
	./build/test_pfh2h
	python3 tests/test_tree_builder.py
	python3 tests/test_upi.py ./solver
	python3 viewer/test_server.py ./solver
	python3 -I ml/loader.py
	python3 -I ml/tests/test_features.py

test-big: solver build/test_eval build/test_range build/test_categories build/test_solver build/test_pfh2h
	./build/test_eval
	./build/test_range
	./build/test_categories
	./build/test_solver
	./build/test_pfh2h
	python3 tests/test_tree_builder.py
	python3 tests/test_upi.py ./solver --big
	python3 viewer/test_server.py ./solver
	python3 -I ml/loader.py
	python3 -I ml/tests/test_features.py

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

# ----- release build: LTO + PGO -----------------------------------------------
# `make release` produces ./solver-release without touching the default ./solver
# or the default build flags:
#   1. compile an instrumented binary into build/pgo/gen/,
#   2. run bench/train_pgo.sh (tests/pio_compare/bench_flop.txt plus the bench
#      river/turn trees, 1 thread and all threads) to collect a profile,
#   3. compile the final binary with LTO, the profile, and the arch flags below.
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
PGO_GEN  := -fprofile-instr-generate
PGO_USE  := -fprofile-instr-use=$(CURDIR)/$(PGO_DIR)/merged.profdata
LTO_FLAG := -flto=thin
else
PGO_GEN  := -fprofile-generate=$(CURDIR)/$(PGO_DIR)/gcc
PGO_USE  := -fprofile-use=$(CURDIR)/$(PGO_DIR)/gcc -fprofile-correction
LTO_FLAG := -flto=auto
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

$(PGO_DIR)/opt/%.o: src/%.cpp $(PGO_DEPS) $(PGO_DIR)/trained.stamp | $(PGO_DIR)/opt
	$(CXX) $(RELEASE_CXXFLAGS) $(LTO_FLAG) $(PGO_USE) -c $< -o $@

solver-release: $(OPT_OBJ)
	$(CXX) $(RELEASE_CXXFLAGS) $(LTO_FLAG) $(PGO_USE) $^ -o $@ $(LDLIBS)

clean:
	rm -rf build solver solver-release

.PHONY: all test test-big windows bench release clean
