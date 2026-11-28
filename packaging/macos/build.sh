#!/bin/sh
# Builds OpenSolver.app and the installer package around it (#479). Run through `make macos-pkg`,
# which builds the solver for it first and passes VERSION and MACOS_MIN.
#
#   packaging/macos/build.sh            stage the app, check that it runs, write dist/OpenSolver-<version>-macos-arm64.pkg
#   packaging/macos/build.sh --files    print the repo files that go into the app (tests/test_packaging.py reads this)
#
# The app carries a Python of its own, so that it runs on a Mac with no developer tools: a
# python-build-standalone build (https://github.com/astral-sh/python-build-standalone), pinned
# below by version and checksum. It is downloaded once into build/macos/cache; to build offline,
# put the tarball there or point PYTHON_TARBALL at it. The checksum is checked either way.
#
# Nothing here is signed with a Developer ID: the app is ad-hoc signed (Apple Silicon runs nothing
# unsigned) and the package is not signed at all. See the README for what that means for opening it.
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/packaging/macos"
cd "$ROOT"

PY_VERSION=3.13.18
PY_RELEASE=20261110
PY_SHA256=7a2384641584ab9984221fcb0fc9265df465922af71be71410529e2a9c3f0540
PY_NAME="cpython-$PY_VERSION+$PY_RELEASE-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PY_RELEASE/cpython-$PY_VERSION%2B$PY_RELEASE-aarch64-apple-darwin-install_only_stripped.tar.gz"

BUNDLE_ID=com.wsolver.WSolver

# What the viewer needs at run time: its modules and its page, and the two tools it imports. Not
# the tests, nor what is only there for them (static/package.json tells Node the page's files are
# modules). From git where there is one, so that a stray local file never ships; a source tarball
# has no git, and no stray files either.
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

: "${VERSION:?run this through make macos-app or macos-pkg}"
: "${MACOS_MIN:?run this through make macos-app or macos-pkg}"
SOLVER="${SOLVER:-build/macos/solver}"
[ "$(uname -s)" = Darwin ] || { echo "the macOS package is built on macOS"; exit 1; }
[ "$(uname -m)" = arm64 ] || { echo "the macOS package is built on an Apple Silicon Mac"; exit 1; }
[ -x "$SOLVER" ] || { echo "no solver at $SOLVER: run make macos-pkg"; exit 1; }

# The files are picked by what git tracks, but copied as they are here: say so when that differs
# from what is committed (make dist, which packs the commit itself, warns the other way round).
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && ! git diff --quiet HEAD -- src viewer tools packaging Makefile; then
  echo "WARNING: uncommitted changes to tracked files ARE in this package:"
  git diff --name-only HEAD -- src viewer tools packaging Makefile | sed 's/^/  /'
fi

OUT=build/macos
STAGE="$OUT/stage"
APP="$STAGE/OpenSolver.app"
RES="$APP/Contents/Resources"
PKG="dist/OpenSolver-$VERSION-macos-arm64.pkg"
export COPYFILE_DISABLE=1   # no ._ files in what is copied or packed
umask 022

# ----- the Python runtime ------------------------------------------------------------------------
TARBALL="${PYTHON_TARBALL:-$OUT/cache/$PY_NAME}"
if [ ! -f "$TARBALL" ]; then
  mkdir -p "$(dirname "$TARBALL")"
  echo "downloading $PY_NAME"
  curl -fL --retry 3 -o "$TARBALL.part" "$PY_URL"
  mv "$TARBALL.part" "$TARBALL"
fi
got=$(shasum -a 256 "$TARBALL" | cut -d' ' -f1)
[ "$got" = "$PY_SHA256" ] || { echo "$TARBALL: sha256 is $got, expected $PY_SHA256"; exit 1; }

# ----- the app -----------------------------------------------------------------------------------
rm -rf "$STAGE"
mkdir -p "$APP/Contents/MacOS" "$RES"

clang -fobjc-arc -O2 -Wall -Wextra -arch arm64 -mmacosx-version-min="$MACOS_MIN" \
  -framework Cocoa -framework WebKit "$HERE/launcher.m" -o "$APP/Contents/MacOS/OpenSolver"

REVISION=$(git rev-parse HEAD 2>/dev/null || true)
# When the source is from (its commit's time): what the app's update check compares (launcher.m)
BUILT=$(git log -1 --format=%ct HEAD 2>/dev/null || echo 0)
[ "$BUILT" != 0 ] || echo "WARNING: no git here, so this build has no build time: it will never offer an update, nor be offered as one"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@MACOS_MIN@/$MACOS_MIN/g" -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" \
  -e "s/@REVISION@/$REVISION/g" -e "s/@BUILT@/$BUILT/g" \
  "$HERE/Info.plist.in" > "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

ICONSET="$OUT/AppIcon.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for px in 16 32 128 256 512; do
  sips -s format png -z $px $px "$HERE/icon.svg" --out "$ICONSET/icon_${px}x${px}.png" >/dev/null
  sips -s format png -z $((px * 2)) $((px * 2)) "$HERE/icon.svg" --out "$ICONSET/icon_${px}x${px}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"

install -m 755 "$SOLVER" "$RES/solver"
files | while IFS= read -r f; do
  mkdir -p "$RES/$(dirname "$f")"
  install -m 644 "$f" "$RES/$f"
done

tar -xzf "$TARBALL" -C "$RES"   # -> Resources/python
PY="$RES/python"
PYLIB="$PY/lib/python${PY_VERSION%.*}"
# What a viewer has no use for: Tcl/Tk and the modules for it, pip, the test suites, what is for
# building against this Python. (bin/python3 is linked statically: it does not load libpython.)
rm -rf "$PY/include" "$PY/share" "$PY/lib/pkgconfig" "$PY"/lib/libpython*.dylib \
       "$PY"/lib/libtcl* "$PY"/lib/tcl* "$PY"/lib/tk* "$PY"/lib/itcl* "$PY"/lib/thread* \
       "$PYLIB/site-packages" "$PYLIB/ensurepip" "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/turtledemo" \
       "$PYLIB/test" "$PYLIB/lib2to3" "$PYLIB"/config-* "$PYLIB"/lib-dynload/_tkinter.*
find "$PY/bin" -mindepth 1 -not -name python3 -not -name "python${PY_VERSION%.*}" -delete
find "$PYLIB" -type d -name __pycache__ -prune -exec rm -rf {} +
mkdir -p "$PYLIB/site-packages"   # site looks for it

chmod -R u+w,go-w,a+rX "$APP"
xattr -cr "$APP"
codesign --force --deep --sign - "$APP" >"$OUT/codesign.log" 2>&1 || { cat "$OUT/codesign.log"; exit 1; }
codesign --verify --deep --strict "$APP"

# ----- does it run? ------------------------------------------------------------------------------
# With the app's own Python, in an empty environment, as it will on a Mac that has nothing else.
env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" "$PY/bin/python3" -B -E -s "$HERE/smoke.py" "$APP" "$VERSION"

if [ "${1:-}" = "--app" ]; then
  echo "$APP"
  exit 0
fi

# ----- the package -------------------------------------------------------------------------------
mkdir -p dist "$OUT/pkg"
# Not relocatable: left to itself the installer puts the app wherever it finds another copy with
# this bundle id (the one just staged in build/, say) instead of in /Applications.
cat > "$OUT/component.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<array>
  <dict>
    <key>RootRelativeBundlePath</key><string>OpenSolver.app</string>
    <key>BundleIsRelocatable</key><false/>
    <key>BundleIsVersionChecked</key><false/>
    <key>BundleHasStrictIdentifier</key><true/>
    <key>BundleOverwriteAction</key><string>upgrade</string>
  </dict>
</array>
</plist>
PLIST
rm -f "$OUT/pkg/OpenSolver.pkg"
pkgbuild --root "$STAGE" --component-plist "$OUT/component.plist" --install-location /Applications \
  --identifier "$BUNDLE_ID.pkg" --version "$VERSION" --ownership recommended "$OUT/pkg/OpenSolver.pkg" >/dev/null
sed -e "s/@VERSION@/$VERSION/g" -e "s/@MACOS_MIN@/$MACOS_MIN/g" -e "s/@BUNDLE_ID@/$BUNDLE_ID/g" \
  "$HERE/distribution.xml.in" > "$OUT/distribution.xml"
rm -rf "$OUT/resources"
mkdir -p "$OUT/resources"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@MACOS_MIN@/$MACOS_MIN/g" "$HERE/welcome.html" > "$OUT/resources/welcome.html"
cp "$HERE/conclusion.html" "$OUT/resources/"
rm -f "$PKG"
productbuild --distribution "$OUT/distribution.xml" --package-path "$OUT/pkg" --resources "$OUT/resources" "$PKG" >/dev/null
(cd dist && shasum -a 256 "$(basename "$PKG")" > "$(basename "$PKG").sha256")
# The update manifest the installed apps read (launcher.m, "updates"): `make publish-macos-pkg`
# puts it in installers/macos/latest.txt with the package.
{
  echo "version $VERSION"
  echo "built $BUILT"
  echo "revision $REVISION"
  echo "file $(basename "$PKG")"
  echo "sha256 $(cut -d' ' -f1 < "$PKG.sha256")"
} > dist/macos-latest.txt
echo "$PKG"
cat "$PKG.sha256"
