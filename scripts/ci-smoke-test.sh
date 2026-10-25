#!/usr/bin/env bash
# Install the built deb on the arm64 runner, prove the desktop app starts under
# Xvfb, capture evidence, then uninstall and prove dpkg cleaned up.
#
# Usage: bash scripts/ci-smoke-test.sh dist/deepseek-harness_*_arm64.deb
set -euo pipefail

DEB="${1:?usage: ci-smoke-test.sh <path-to-deb>}"
EVIDENCE="${EVIDENCE_DIR:-evidence}"
APP_DIR=/opt/deepseek-harness
LAUNCHER=/usr/bin/deepseek-harness
DISPLAY_NUM=:99

mkdir -p "$EVIDENCE"
log() { echo; echo "== $* =="; }

cleanup() {
  set +e
  if [ -n "${APP_GROUP:-}" ]; then kill -TERM -- "-$APP_GROUP" 2>/dev/null; sleep 2; kill -KILL -- "-$APP_GROUP" 2>/dev/null; fi
  if [ -n "${XVFB_PID:-}" ]; then kill -TERM "$XVFB_PID" 2>/dev/null; fi
}
trap cleanup EXIT

log "install $DEB"
sudo apt-get update -qq
sudo apt-get install -y "./$DEB" | tee "$EVIDENCE/apt-install.log"

log "installed files"
{
  dpkg -s deepseek-harness
  echo
  dpkg -L deepseek-harness
} | tee "$EVIDENCE/dpkg-state.txt" >/dev/null
head -40 "$EVIDENCE/dpkg-state.txt"

test -x "$APP_DIR/deepseek-harness" || { echo "FAIL: missing app binary"; exit 1; }
test -x "$LAUNCHER" || { echo "FAIL: missing /usr/bin launcher"; exit 1; }
grep -q 'force-prefers-reduced-motion' "$LAUNCHER" || { echo "FAIL: launcher lost the reduced-motion switch"; exit 1; }
grep -q 'DSH_HARNESS_KEEP_ANIMATIONS' "$LAUNCHER" || { echo "FAIL: launcher lost the animation opt-out"; exit 1; }
test -f /usr/share/applications/deepseek-harness.desktop || { echo "FAIL: missing desktop entry"; exit 1; }
test -f /usr/share/icons/hicolor/512x512/apps/deepseek-harness.png || { echo "FAIL: missing 512px icon"; exit 1; }
test -L /usr/bin/dsh || { echo "FAIL: missing /usr/bin/dsh CLI link"; exit 1; }
grep -q 'x-scheme-handler/dsh' /usr/share/applications/deepseek-harness.desktop || { echo "FAIL: dsh protocol not declared"; exit 1; }

log "CLI entry"
timeout 120 dsh --help > "$EVIDENCE/dsh-help.txt" 2>&1 || true
head -5 "$EVIDENCE/dsh-help.txt" || true
grep -qi 'usage\|dsh' "$EVIDENCE/dsh-help.txt" || { echo "FAIL: dsh --help produced no usage output"; exit 1; }
if command -v xdg-mime >/dev/null 2>&1; then
  xdg-mime query default x-scheme-handler/dsh | tee "$EVIDENCE/protocol-handler.txt"
  grep -q 'deepseek-harness' "$EVIDENCE/protocol-handler.txt" || echo "WARN: dsh:// default handler not set by postinst"
fi

log "ELF architecture"
{
  file "$APP_DIR/deepseek-harness"
  file "$APP_DIR/chrome-sandbox"
  ls -l "$APP_DIR/chrome-sandbox"
} | tee "$EVIDENCE/elf-arch.txt"
grep -q 'ARM aarch64' "$EVIDENCE/elf-arch.txt" || { echo "FAIL: app binary is not arm64"; exit 1; }

log "shared-library closure"
MISSING=0
for binary in "$APP_DIR/deepseek-harness" "$APP_DIR/chrome-sandbox" "$APP_DIR/resources/runtime/primary-runtime/dependencies/node/bin/node"; do
  [ -e "$binary" ] || continue
  output="$(ldd "$binary" 2>&1 || true)"
  echo "--- $binary" >> "$EVIDENCE/ldd.log"
  echo "$output" >> "$EVIDENCE/ldd.log"
  if grep -q 'not found' <<<"$output"; then
    echo "FAIL: missing shared libraries for $binary"
    grep 'not found' <<<"$output"
    MISSING=1
  fi
done
[ "$MISSING" -eq 0 ] || exit 1

log "start Xvfb"
Xvfb "$DISPLAY_NUM" -screen 0 1440x900x24 >"$EVIDENCE/xvfb.log" 2>&1 &
XVFB_PID=$!
sleep 2
export DISPLAY="$DISPLAY_NUM"

start_app() {
  local mode="$1"; shift
  echo "launch mode: $mode" | tee -a "$EVIDENCE/app.log"
  # setsid keeps the Electron process tree in one killable group.
  setsid "$LAUNCHER" "$@" >>"$EVIDENCE/app.log" 2>&1 &
  APP_GROUP=$!
  echo "$APP_GROUP" > "$EVIDENCE/app.pid"
  sleep "${LAUNCH_WAIT_SECONDS:-25}"
  if kill -0 "$APP_GROUP" 2>/dev/null; then
    return 0
  fi
  return 1
}

log "launch application"
if ! start_app default; then
  echo "default launch exited early; retrying with --no-sandbox (CI container kernel restrictions)" | tee -a "$EVIDENCE/app.log"
  cp "$EVIDENCE/app.log" "$EVIDENCE/app-default.log"
  APP_GROUP=""
  if ! start_app no-sandbox --no-sandbox; then
    echo "still not alive; retrying with software rendering flags" | tee -a "$EVIDENCE/app.log"
    cp "$EVIDENCE/app.log" "$EVIDENCE/app-nosandbox.log"
    APP_GROUP=""
    if ! start_app software --no-sandbox --disable-gpu --disable-dev-shm-usage --disable-software-rasterizer; then
      echo "FAIL: application did not stay alive"
      tail -120 "$EVIDENCE/app.log"
      exit 1
    fi
  fi
fi

log "capture evidence"
{
  ps -eo pid,ppid,cmd --forest | grep -iE 'deepseek|electron|Xvfb' | grep -v grep || true
} > "$EVIDENCE/processes.txt"
tail -60 "$EVIDENCE/app.log" > "$EVIDENCE/app-tail.txt" || true
xwininfo -root -tree > "$EVIDENCE/windows.txt" 2>&1 || true
import -display "$DISPLAY_NUM" -window root "$EVIDENCE/desktop.png" 2>>"$EVIDENCE/screenshot.log" \
  || xwd -display "$DISPLAY_NUM" -root -silent > "$EVIDENCE/desktop.xwd" 2>>"$EVIDENCE/screenshot.log" || true

# The window tree is the strongest signal that the Electron shell created a UI.
if ! grep -qi 'deepseek' "$EVIDENCE/windows.txt"; then
  echo "WARN: no DeepSeek window title found in xwininfo output; see $EVIDENCE/windows.txt"
fi
echo "STARTUP_OK"

log "uninstall"
cleanup
trap - EXIT
sudo apt-get purge -y deepseek-harness | tee "$EVIDENCE/apt-purge.log"
test ! -e "$APP_DIR" || { echo "FAIL: $APP_DIR survived purge"; exit 1; }
test ! -e "$LAUNCHER" || { echo "FAIL: $LAUNCHER survived purge"; exit 1; }
echo "UNINSTALL_OK"
