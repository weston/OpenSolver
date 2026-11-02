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
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

test: solver build/test_eval build/test_range build/test_categories build/test_solver build/test_pfh2h
	./build/test_eval
	./build/test_range
	./build/test_categories
	./build/test_solver
	./build/test_pfh2h
	python3 tests/test_tree_builder.py
	python3 tests/test_upi.py ./solver

test-big: solver build/test_eval build/test_range build/test_categories build/test_solver build/test_pfh2h
	./build/test_eval
	./build/test_range
	./build/test_categories
	./build/test_solver
	./build/test_pfh2h
	python3 tests/test_tree_builder.py
	python3 tests/test_upi.py ./solver --big

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

clean:
	rm -rf build solver

.PHONY: all test test-big windows bench clean
