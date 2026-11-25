#!/bin/zsh
set -e

REPO_URL="git@github.com:weston/WSolver.git"
APP_DIR="$HOME/.wsolver/app"
BRANCH="main"
INSTALLED_APP="/Applications/WSolver.app"

if [ ! -d "$APP_DIR/.git" ]; then
  echo "Cloning WSolver ..."
  mkdir -p "$(dirname "$APP_DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
echo "Downloading the latest version ..."
git fetch origin "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"
REVISION=$(git rev-parse HEAD)
INSTALLED_REVISION=$(/usr/libexec/PlistBuddy -c 'Print :WSolverSourceRevision' \
  "$INSTALLED_APP/Contents/Info.plist" 2>/dev/null || true)

if [ "$REVISION" != "$INSTALLED_REVISION" ]; then
  echo "Building WSolver ..."
  make -j"$(sysctl -n hw.ncpu)" macos-app

  echo "Installing WSolver ..."
  INSTALL_STAGE=$(mktemp -d /Applications/.wsolver-install.XXXXXX)
  trap 'rm -rf "$INSTALL_STAGE"' EXIT
  ditto build/macos/stage/WSolver.app "$INSTALL_STAGE/WSolver.app"
  codesign --verify --deep --strict "$INSTALL_STAGE/WSolver.app"
  if [ -d "$INSTALLED_APP" ]; then
    osascript -e 'if application "WSolver" is running then tell application "WSolver" to quit'
    for i in {1..60}; do
      pgrep -f '^/Applications/WSolver.app/Contents/MacOS/WSolver($| )' >/dev/null || break
      sleep 0.25
    done
    if pgrep -f '^/Applications/WSolver.app/Contents/MacOS/WSolver($| )' >/dev/null; then
      echo "WSolver is still running. Installation stopped."
      exit 1
    fi
    mv "$INSTALLED_APP" "$INSTALL_STAGE/previous.app"
  fi
  mv "$INSTALL_STAGE/WSolver.app" "$INSTALLED_APP"
fi

echo "Opening WSolver ..."
open "$INSTALLED_APP"
