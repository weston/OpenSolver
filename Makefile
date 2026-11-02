# OpenUPI-Solver build. `make` builds ./solver; `make test` runs the unit and integration tests.
CXX      ?= clang++
CXXFLAGS ?= -std=c++20 -O3 -mcpu=native -Wall -Wextra -DNDEBUG
LDLIBS   := -lz -lpthread
SRC      := src/cards.cpp src/eval.cpp src/range.cpp src/action_tree.cpp src/game.cpp src/solver.cpp \
            src/parallel.cpp src/equity.cpp src/categories.cpp src/upi.cpp src/upi_nodes.cpp src/upi_save.cpp
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

test: solver build/test_eval build/test_range build/test_categories build/test_solver
	./build/test_range
	./build/test_categories
	./build/test_solver
	python3 tests/test_upi.py ./solver

clean:
	rm -rf build solver

.PHONY: all test clean
