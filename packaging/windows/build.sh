#!/bin/sh
# Builds OpenSolver for Windows and its setup program (#565), the counterpart of packaging/macos/build.sh.
# Run through `make windows-installer` in an MSYS2 UCRT64 shell, which builds the solver for it
# first and passes VERSION, WIN_ARCH and SOLVER.
#
#   packaging/windows/build.sh            stage the app, check that it runs, write dist/OpenSolver-<version>-windows-x64-setup.exe
#   packaging/windows/build.sh --app      stage the app and check that it runs, no installer
#   packaging/windows/build.sh --files    print the repo files that go into the app (tests/test_packaging.py reads this)
#
# What it needs besides the compiler that built the solver: the NSIS compiler
# (pacman -S mingw-w64-ucrt-x86_64-nsis), curl, and a Python to draw the icon with (any 3.x).
#
# The app carries a Python of its own, so that it runs on a PC with nothing else installed: a
# python-build-standalone build (https://github.com/astral-sh/python-build-standalone), pinned
# below by version and checksum (the same release as the macOS app's). It is downloaded once into
# build/windows/cache; to build offline, put the tarball there or point PYTHON_TARBALL at it. The
# checksum is checked either way.
#
# Nothing here is code-signed: Windows SmartScreen warns about the setup program the first time
# it is run. See the README for what that means for opening it.
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/packaging/windows"
cd "$ROOT"

PY_VERSION=3.13.18
PY_RELEASE=20261110
PY_SHA256=2e0fed09d48989f64c9305da9ff2d37a41c0d13ab2aa6283405b2b620dfbf805
PY_NAME="cpython-$PY_VERSION+$PY_RELEASE-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/cpython-$PY_VERSION%2B$PY_RELEASE-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"

# What the viewer needs at run time: the same files as the macOS app's (packaging/macos/build.sh),
# from git where there is one, so that a stray local file never ships.
files() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git ls-files viewer tools/tree_builder.py tools/jobs.py tools/preflop.py flops
  else
    find viewer tools/tree_builder.py tools/jobs.py tools/preflop.py flops -type f -not -name '*.pyc' -not -name .DS_Store
  fi | grep -v -e '^viewer/test_' -e '^viewer/jsc_prelude\.js$' -e '^viewer/static/package\.json$' | LC_ALL=C sort
}

if [ "${1:-}" = "--files" ]; then
  files
  exit 0
fi

: "${VERSION:?run this through make windows-installer}"
: "${SOLVER:?run this through make windows-installer}"
WIN_ARCH="${WIN_ARCH:-}"
case "$(uname -s)" in
  MINGW*|MSYS*|UCRT*|CLANG*) ;;
  *) echo "the Windows installer is built on Windows, in an MSYS2 UCRT64 shell"; exit 1 ;;
esac
[ -f "$SOLVER" ] || { echo "no solver at $SOLVER: run make windows-installer"; exit 1; }
command -v makensis >/dev/null || { echo "makensis not found: pacman -S mingw-w64-ucrt-x86_64-nsis"; exit 1; }
CXX="${CXX:-g++}"
WINDRES="${WINDRES:-windres}"
PYTHON_HOST="${PYTHON_HOST:-$(command -v python3 || command -v python)}"

# The solver must run on a PC with no MSYS2: nothing but Windows' own DLLs.
foreign=$(objdump -p "$SOLVER" | sed -n 's/^[[:space:]]*DLL Name: //p' | tr -d '\r' |
  grep -v -i -x -e 'kernel32.dll' -e 'msvcrt.dll' -e 'ucrtbase.dll' -e 'api-ms-win-.*\.dll' -e 'advapi32.dll' \
    -e 'user32.dll' -e 'ws2_32.dll' -e 'shell32.dll' -e 'ole32.dll' -e 'bcrypt.dll' -e 'ntdll.dll' -e 'opencl.dll' || true)
[ -z "$foreign" ] || { echo "$SOLVER needs DLLs a PC without MSYS2 does not have: $foreign (link it with -static)"; exit 1; }

# The files are picked by what git tracks, but copied as they are here: say so when that differs
# from what is committed.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && ! git diff --quiet HEAD -- src viewer tools packaging Makefile; then
  echo "WARNING: uncommitted changes to tracked files ARE in this installer:"
  git diff --name-only HEAD -- src viewer tools packaging Makefile | sed 's/^/  /'
fi

OUT=build/windows
APP="$OUT/stage/OpenSolver"
SETUP="dist/OpenSolver-$VERSION-windows-x64-setup.exe"

# ----- the Python runtime ------------------------------------------------------------------------
TARBALL="${PYTHON_TARBALL:-$OUT/cache/$PY_NAME}"
if [ ! -f "$TARBALL" ]; then
  mkdir -p "$(dirname "$TARBALL")"
  echo "downloading $PY_NAME"
  curl -fL --retry 3 -o "$TARBALL.part" "$PY_URL"
  mv "$TARBALL.part" "$TARBALL"
fi
got=$(sha256sum "$TARBALL" | cut -d' ' -f1)
[ "$got" = "$PY_SHA256" ] || { echo "$TARBALL: sha256 is $got, expected $PY_SHA256"; exit 1; }

# ----- the app -----------------------------------------------------------------------------------
rm -rf "$OUT/stage"
mkdir -p "$APP" "$OUT/res"

# The version as Windows writes it: four numbers
VERSION4=$(echo "$VERSION" | awk -F. '{ printf "%d.%d.%d.0", $1, $2, $3 }')
VERSIONCOMMA=$(echo "$VERSION4" | tr . ,)
"$PYTHON_HOST" "$HERE/make_icon.py" viewer/static/favicon.svg "$OUT/res/icon.ico"
cp "$HERE/launcher.manifest" "$OUT/res/"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@VERSION4@/$VERSION4/g" -e "s/@VERSIONCOMMA@/$VERSIONCOMMA/g" \
  "$HERE/launcher.rc.in" > "$OUT/res/launcher.rc"
"$WINDRES" -O coff "$OUT/res/launcher.rc" -o "$OUT/res/launcher.res"
# The launcher says so when the CPU is too old for the solver (it would crash, not explain).
ISA=
case "$WIN_ARCH" in *x86-64-v3*) ISA='-DOPENSOLVER_ISA="x86-64-v3"' ;; esac
# shellcheck disable=SC2086
"$CXX" -std=c++20 -O2 -Wall -Wextra -municode -mwindows -static -D_WIN32_WINNT=0x0A00 $ISA \
  "$HERE/launcher.cpp" "$OUT/res/launcher.res" -o "$APP/OpenSolver.exe" \
  -lws2_32 -lshell32 -lole32 -luuid -lcomctl32 -ladvapi32 -luser32
strip "$APP/OpenSolver.exe"

install -m 755 "$SOLVER" "$APP/solver.exe"
files | while IFS= read -r f; do
  mkdir -p "$APP/$(dirname "$f")"
  install -m 644 "$f" "$APP/$f"
done

tar -xzf "$TARBALL" -C "$APP"   # -> python
PY="$APP/python"
PYLIB="$PY/Lib"
# What a viewer has no use for: Tcl/Tk and the modules for it, pip, the test suites and the
# modules only they import, what is for building against this Python, the windowless python.
rm -rf "$PY/include" "$PY/libs" "$PY/Scripts" "$PY/tcl" "$PY/pythonw.exe" \
       "$PY"/DLLs/_tkinter.pyd "$PY"/DLLs/tcl*.dll "$PY"/DLLs/tk*.dll "$PY"/DLLs/_test*.pyd "$PY"/DLLs/_ctypes_test.pyd \
       "$PYLIB/site-packages" "$PYLIB/ensurepip" "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/turtledemo" \
       "$PYLIB/test" "$PYLIB/lib2to3"
find "$PYLIB" -type d -name __pycache__ -prune -exec rm -rf {} +
mkdir -p "$PYLIB/site-packages"   # site looks for it

# ----- does it run? ------------------------------------------------------------------------------
# With the app's own Python and launcher, as it runs on a PC that has nothing else.
"$PY/python.exe" -B -E -s "$HERE/smoke.py" "$APP" "$VERSION"

if [ "${1:-}" = "--app" ]; then
  echo "$APP"
  exit 0
fi

# ----- the installer -----------------------------------------------------------------------------
mkdir -p dist
rm -f "$SETUP"
makensis -V2 -DVERSION="$VERSION" -DVERSION4="$VERSION4" -DSTAGE="$(cygpath -w "$ROOT/$APP")" \
  -DOUTFILE="$(cygpath -w "$ROOT/$SETUP")" -DICON="$(cygpath -w "$ROOT/$OUT/res/icon.ico")" \
  "$(cygpath -w "$HERE/installer.nsi")"
name=$(basename "$SETUP")
(cd dist && printf '%s  %s\n' "$(sha256sum < "$name" | cut -d' ' -f1)" "$name" > "$name.sha256")
echo "$SETUP"
cat "$SETUP.sha256"
