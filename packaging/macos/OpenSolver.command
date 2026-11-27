#!/bin/zsh
set -e

REPO_URL="git@github.com:weston/WSolver.git"
APP_DIR="$HOME/.wsolver/app"
BRANCH="main"
INSTALLED_APP="/Applications/OpenSolver.app"

if [ ! -d "$APP_DIR/.git" ]; then
  echo "Cloning OpenSolver ..."
  mkdir -p "$(dirname "$APP_DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
echo "Downloading the latest version ..."
git fetch origin "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"
REVISION=$(git rev-parse HEAD)
INSTALLED_REVISION=$(/usr/libexec/PlistBuddy -c 'Print :OpenSolverSourceRevision' \
  "$INSTALLED_APP/Contents/Info.plist" 2>/dev/null || true)

if [ "$REVISION" != "$INSTALLED_REVISION" ]; then
  echo "Building OpenSolver ..."
  make -j"$(sysctl -n hw.ncpu)" macos-app

  echo "Installing OpenSolver ..."
  INSTALL_STAGE=$(mktemp -d /Applications/.wsolver-install.XXXXXX)
  trap 'rm -rf "$INSTALL_STAGE"' EXIT
  ditto build/macos/stage/OpenSolver.app "$INSTALL_STAGE/OpenSolver.app"
  codesign --verify --deep --strict "$INSTALL_STAGE/OpenSolver.app"
  if [ -d "$INSTALLED_APP" ]; then
    osascript -e 'if application "OpenSolver" is running then tell application "OpenSolver" to quit'
    for i in {1..60}; do
      pgrep -f '^/Applications/OpenSolver.app/Contents/MacOS/OpenSolver($| )' >/dev/null || break
      sleep 0.25
    done
    if pgrep -f '^/Applications/OpenSolver.app/Contents/MacOS/OpenSolver($| )' >/dev/null; then
      echo "OpenSolver is still running. Installation stopped."
      exit 1
    fi
    mv "$INSTALLED_APP" "$INSTALL_STAGE/previous.app"
  fi
  mv "$INSTALL_STAGE/OpenSolver.app" "$INSTALLED_APP"
fi

echo "Opening OpenSolver ..."
open "$INSTALLED_APP"
