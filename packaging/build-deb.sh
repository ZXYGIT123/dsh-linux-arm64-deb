#!/usr/bin/env bash
# build-deb.sh - assemble DeepSeek Harness Linux arm64 .deb packages.
#
# Input is the unpacked directory produced by electron-builder's "--linux dir"
# target (the same tree electron-builder would feed into AppImage/deb targets):
#
#   <app-dir>/
#     deepseek-harness          Electron application executable
#     chrome-sandbox            setuid sandbox helper (root:root 4755 in the deb)
#     resources/app.asar
#     resources/runtime/...
#     locales/, *.so, ...
#
# The script stages the Debian filesystem layout, generates all metadata and
# calls dpkg-deb. It never needs network access, npm, pip or fakeroot.
#
# Usage:
#   bash packaging/build-deb.sh \
#     --app-dir dist/linux-arm64-unpacked \
#     --version 0.2.1~rc.3 \
#     --arch arm64 \
#     --output dist \
#     --icon packaging/icons/deepseek-harness.png
#
# See README.md next to this script for the full option list.
#
# Exit status is non-zero on any failure; every step prints a "[build-deb]"
# progress line so CI logs are easy to follow.

set -eu
# POSIX-compatible on purpose (dash syntax-checked); enable pipefail under bash.
if [ -n "${BASH_VERSION:-}" ]; then
  set -o pipefail
fi
umask 022

PROG="build-deb.sh"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
TOOLS_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../tools" && pwd -P)

# ---------------------------------------------------------------------------
# defaults (override on the command line)
# ---------------------------------------------------------------------------

APP_DIR=""
VERSION=""
ARCH="arm64"
OUTPUT="."
MAINTAINER="DeepSeek Harness Linux community build <dsh-linux@users.noreply.github.com>"
HOMEPAGE="https://github.com/deepseek-ai/deepseek-harness"
PACKAGE_NAME="deepseek-harness"
EXE_NAME="deepseek-harness"
SECTION="devel"
PRIORITY="optional"
COMPRESSION="auto"
ICON=""
ICON_256=""
ICON_512=""
EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"
KEEP=0
RUN_VERIFY=0
ALLOW_MISSING_RUNTIME=0
ALLOW_MISSING_SANDBOX=0
DEPENDS_OVERRIDE=""
RECOMMENDS_OVERRIDE=""

# Electron 44 runtime libraries on Ubuntu 24.04 arm64. Pre-24.04 package names
# are listed as alternatives because Ubuntu 24.04 renamed several libraries in
# the time_t 64-bit transition (the "t64" suffix); apt resolves either branch.
DEFAULT_DEPENDS='libasound2t64 | libasound2, libatk-bridge2.0-0t64 | libatk-bridge2.0-0, libatk1.0-0t64 | libatk1.0-0, libatspi2.0-0t64 | libatspi2.0-0, libcairo2, libcups2t64 | libcups2, libdbus-1-3, libdrm2, libexpat1, libfontconfig1, libfreetype6, libgbm1, libglib2.0-0t64 | libglib2.0-0, libgtk-3-0t64 | libgtk-3-0, libnspr4, libnss3, libpango-1.0-0, libsecret-1-0, libuuid1, libx11-6, libx11-xcb1, libxcb1, libxcb-dri3-0, libxcomposite1, libxdamage1, libxext6, libxfixes3, libxkbcommon0, libxrandr2, libxshmfence1, libxss1, libxtst6, libnotify4, xdg-utils'
DEFAULT_RECOMMENDS='fonts-noto-color-emoji, fonts-liberation, libvulkan1'

# Set by attempt_build().
ACTUAL_COMPRESSION=""

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

usage() {
  cat <<'USAGE'
build-deb.sh - assemble the DeepSeek Harness Linux .deb from an unpacked app.

Required:
  --app-dir DIR        electron-builder "--linux dir" output directory
                       (contains <exe>, resources/app.asar, chrome-sandbox, ...)
  --version VERSION    package version, e.g. 0.2.1~rc.3 (a leading "v" and
                       dashes are normalised; dashes become "~")

Optional:
  --arch ARCH          Debian architecture: arm64 (default) or amd64
  --output PATH        output .deb file, or directory (default: .)
  --package-name NAME  Debian package name (default: deepseek-harness)
  --exe-name NAME      executable inside --app-dir (default: deepseek-harness)
  --maintainer TEXT    Maintainer control field
  --homepage URL       Homepage control field
  --section NAME       Section control field (default: devel)
  --priority NAME      Priority control field (default: optional)
  --depends LIST       override the generated Depends list
  --recommends LIST    override the generated Recommends list
  --compression TYPE   auto (default, zstd then xz), zstd, xz, gzip or none
  --icon PNG           source icon, scaled to 256x256 and 512x512
  --icon-256 PNG       pre-sized 256 icon (scaled from this if --icon is absent)
  --icon-512 PNG       pre-sized 512 icon (scaled from this if --icon is absent)
  --epoch UNIX_TIME    fixed SOURCE_DATE_EPOCH/mtime (default: 1700000000,
                       or $SOURCE_DATE_EPOCH when set)
  --verify             run ../tools/verify_deb.py on the produced .deb
  --keep               keep the staging directory for debugging
  --allow-missing-runtime      do not fail when resources/runtime is absent
  --allow-missing-sandbox      do not fail when chrome-sandbox is absent
  -h, --help           show this help

Environment:
  PYTHON               python interpreter used for icon scaling and --verify
                       (auto-detected: python3, then python)
  SOURCE_DATE_EPOCH    default for --epoch
  LIBZSTD              only used by verify_deb.py when Python lacks zstd
USAGE
}

log() {
  printf '[build-deb] %s\n' "$*"
}

warn() {
  printf '[build-deb] WARN: %s\n' "$*" >&2
}

die() {
  printf '[build-deb] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "required command not found: $1"
  fi
}

# Escape a string for the replacement part of `sed 's|...|REPLACEMENT|g'`.
escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

require_clean_value() {
  # Reject empty values and embedded newlines before writing them into control.
  if [ -z "$2" ]; then
    die "$1 must not be empty"
  fi
  case "$2" in
    *"
"*) die "$1 must not contain newlines" ;;
  esac
}

normalize_version() {
  version=$1
  case "$version" in
    v*) version=${version#v} ;;
  esac
  version=$(printf '%s' "$version" | tr -- '-' '~')
  case "$version" in
    "") die "--version must not be empty" ;;
    [0-9]*) : ;;
    *) die "version '$version' must start with a digit (Debian policy)" ;;
  esac
  case "$version" in
    *[!A-Za-z0-9.+~]*) die "version '$version' contains characters that are not valid in a Debian upstream version" ;;
  esac
  printf '%s' "$version"
}

validate_name() {
  # $1 = option name, $2 = value; used for package and executable names.
  case "$2" in
    "") die "$1 must not be empty" ;;
    *[!A-Za-z0-9._+-]*) die "$1 '$2' may only contain letters, digits, '.', '_', '+' and '-'" ;;
  esac
}

format_changelog_date() {
  if date -u -d "@$1" '+%a, %d %b %Y %H:%M:%S +0000' >/dev/null 2>&1; then
    date -u -d "@$1" '+%a, %d %b %Y %H:%M:%S +0000'
  else
    # BSD/macOS date fallback (not used by CI, kept for completeness).
    date -u -r "$1" '+%a, %d %b %Y %H:%M:%S +0000'
  fi
}

resize_icon() {
  source=$1
  destination=$2
  size=$3
  if [ ! -f "$source" ]; then
    die "icon source does not exist: $source"
  fi
  if [ -n "${PYTHON:-}" ] && [ -f "$TOOLS_DIR/png_resize.py" ]; then
    if "$PYTHON" "$TOOLS_DIR/png_resize.py" "$source" "$destination" "$size" "$size"; then
      return 0
    fi
    warn "png_resize.py failed for '$source'; trying ImageMagick"
  fi
  if command -v magick >/dev/null 2>&1; then
    if magick "$source" -resize "${size}x${size}!" "$destination"; then
      return 0
    fi
  elif command -v convert >/dev/null 2>&1; then
    if convert "$source" -resize "${size}x${size}!" "$destination"; then
      return 0
    fi
  fi
  die "no working PNG resizer: install python3 (preferred) or ImageMagick"
}

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "${KEEP:-0}" -eq 1 ]; then
    log "kept staging directory: ${WORKDIR:-<none>}"
  elif [ -n "${WORKDIR:-}" ] && [ -d "$WORKDIR" ]; then
    rm -rf "$WORKDIR"
  fi
  exit "$status"
}

attempt_build() {
  compression=$1
  rm -f "$WORKDIR/package.deb"
  log "dpkg-deb: building with $compression compression"
  if [ -n "$DPKG_ROOT_FLAG" ]; then
    if dpkg-deb --build "$DPKG_ROOT_FLAG" "--compression=$compression" \
        "$STAGE" "$WORKDIR/package.deb" >"$WORKDIR/dpkg-$compression.log" 2>&1; then
      ACTUAL_COMPRESSION=$compression
      return 0
    fi
  elif dpkg-deb --build "--compression=$compression" \
      "$STAGE" "$WORKDIR/package.deb" >"$WORKDIR/dpkg-$compression.log" 2>&1; then
    ACTUAL_COMPRESSION=$compression
    return 0
  fi
  warn "dpkg-deb failed with $compression compression:"
  sed 's/^/  /' "$WORKDIR/dpkg-$compression.log" >&2 || true
  return 1
}

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --app-dir)
      [ $# -ge 2 ] || die "--app-dir requires a value"
      APP_DIR=$2
      shift 2
      ;;
    --app-dir=*)
      APP_DIR=${1#*=}
      shift
      ;;
    --version)
      [ $# -ge 2 ] || die "--version requires a value"
      VERSION=$2
      shift 2
      ;;
    --version=*)
      VERSION=${1#*=}
      shift
      ;;
    --arch)
      [ $# -ge 2 ] || die "--arch requires a value"
      ARCH=$2
      shift 2
      ;;
    --arch=*)
      ARCH=${1#*=}
      shift
      ;;
    --output)
      [ $# -ge 2 ] || die "--output requires a value"
      OUTPUT=$2
      shift 2
      ;;
    --output=*)
      OUTPUT=${1#*=}
      shift
      ;;
    --package-name)
      [ $# -ge 2 ] || die "--package-name requires a value"
      PACKAGE_NAME=$2
      shift 2
      ;;
    --package-name=*)
      PACKAGE_NAME=${1#*=}
      shift
      ;;
    --exe-name)
      [ $# -ge 2 ] || die "--exe-name requires a value"
      EXE_NAME=$2
      shift 2
      ;;
    --exe-name=*)
      EXE_NAME=${1#*=}
      shift
      ;;
    --maintainer)
      [ $# -ge 2 ] || die "--maintainer requires a value"
      MAINTAINER=$2
      shift 2
      ;;
    --maintainer=*)
      MAINTAINER=${1#*=}
      shift
      ;;
    --homepage)
      [ $# -ge 2 ] || die "--homepage requires a value"
      HOMEPAGE=$2
      shift 2
      ;;
    --homepage=*)
      HOMEPAGE=${1#*=}
      shift
      ;;
    --section)
      [ $# -ge 2 ] || die "--section requires a value"
      SECTION=$2
      shift 2
      ;;
    --section=*)
      SECTION=${1#*=}
      shift
      ;;
    --priority)
      [ $# -ge 2 ] || die "--priority requires a value"
      PRIORITY=$2
      shift 2
      ;;
    --priority=*)
      PRIORITY=${1#*=}
      shift
      ;;
    --depends)
      [ $# -ge 2 ] || die "--depends requires a value"
      DEPENDS_OVERRIDE=$2
      shift 2
      ;;
    --depends=*)
      DEPENDS_OVERRIDE=${1#*=}
      shift
      ;;
    --recommends)
      [ $# -ge 2 ] || die "--recommends requires a value"
      RECOMMENDS_OVERRIDE=$2
      shift 2
      ;;
    --recommends=*)
      RECOMMENDS_OVERRIDE=${1#*=}
      shift
      ;;
    --compression)
      [ $# -ge 2 ] || die "--compression requires a value"
      COMPRESSION=$2
      shift 2
      ;;
    --compression=*)
      COMPRESSION=${1#*=}
      shift
      ;;
    --icon)
      [ $# -ge 2 ] || die "--icon requires a value"
      ICON=$2
      shift 2
      ;;
    --icon=*)
      ICON=${1#*=}
      shift
      ;;
    --icon-256)
      [ $# -ge 2 ] || die "--icon-256 requires a value"
      ICON_256=$2
      shift 2
      ;;
    --icon-256=*)
      ICON_256=${1#*=}
      shift
      ;;
    --icon-512)
      [ $# -ge 2 ] || die "--icon-512 requires a value"
      ICON_512=$2
      shift 2
      ;;
    --icon-512=*)
      ICON_512=${1#*=}
      shift
      ;;
    --epoch)
      [ $# -ge 2 ] || die "--epoch requires a value"
      EPOCH=$2
      shift 2
      ;;
    --epoch=*)
      EPOCH=${1#*=}
      shift
      ;;
    --verify)
      RUN_VERIFY=1
      shift
      ;;
    --keep)
      KEEP=1
      shift
      ;;
    --allow-missing-runtime)
      ALLOW_MISSING_RUNTIME=1
      shift
      ;;
    --allow-missing-sandbox)
      ALLOW_MISSING_SANDBOX=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      die "unknown argument: $1 (see --help)"
      ;;
  esac
done

# ---------------------------------------------------------------------------
# step 1: validate inputs and toolchain
# ---------------------------------------------------------------------------

log "[1/8] validating inputs and toolchain"

[ -n "$APP_DIR" ] || die "--app-dir is required (see --help)"
[ -n "$VERSION" ] || die "--version is required (see --help)"
require_clean_value "--maintainer" "$MAINTAINER"
require_clean_value "--homepage" "$HOMEPAGE"
require_clean_value "--section" "$SECTION"
require_clean_value "--priority" "$PRIORITY"

VERSION=$(normalize_version "$VERSION")
validate_name "--package-name" "$PACKAGE_NAME"
validate_name "--exe-name" "$EXE_NAME"
case "$ARCH" in
  arm64|amd64) : ;;
  *) die "unsupported --arch '$ARCH' (supported: arm64, amd64)" ;;
esac
case "$EPOCH" in
  ''|*[!0-9]*) die "--epoch must be a non-negative integer (seconds since 1970)" ;;
esac
case "$COMPRESSION" in
  auto|zstd|xz|gzip|none) : ;;
  *) die "unsupported --compression '$COMPRESSION' (auto, zstd, xz, gzip, none)" ;;
esac

[ -d "$APP_DIR" ] || die "app directory does not exist: $APP_DIR"
APP_DIR=$(CDPATH= cd -- "$APP_DIR" && pwd -P) || die "cannot enter app directory"

for tool in dpkg-deb find cp chmod touch gzip du md5sum sed sort xargs cut tr date awk mktemp ln; do
  require_command "$tool"
done

# Resolve the output path: a directory unless it ends in .deb.
case "$OUTPUT" in
  *.deb)
    OUT_DIR=$(dirname -- "$OUTPUT")
    DEB_NAME=$(basename -- "$OUTPUT")
    ;;
  *)
    OUT_DIR=$OUTPUT
    DEB_NAME="${PACKAGE_NAME}_${VERSION}_${ARCH}.deb"
    ;;
esac
[ -d "$OUT_DIR" ] || mkdir -p "$OUT_DIR" || die "cannot create output directory: $OUT_DIR"
DEB_FILE="$OUT_DIR/$DEB_NAME"
rm -f "$DEB_FILE"

# Python is used for icon scaling and --verify. It is optional for the build as
# long as ImageMagick is available, but --verify requires it.
PYTHON=${PYTHON:-}
if [ -z "$PYTHON" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  elif command -v python >/dev/null 2>&1; then
    PYTHON=python
  fi
fi
if [ -n "$PYTHON" ]; then
  PYTHON_VERSION=$("$PYTHON" --version 2>&1 | tr -d '\r' || true)
  log "python: $PYTHON (${PYTHON_VERSION:-unknown})"
else
  warn "python3/python not found: falling back to ImageMagick for icon scaling"
fi
if [ "$RUN_VERIFY" -eq 1 ] && [ -z "$PYTHON" ]; then
  die "--verify requires python3 (or python) on PATH"
fi

# dpkg-deb --root-owner-group is required for root:root ownership; Ubuntu 24.04
# ships dpkg 1.22 where it is the default-capable flag. Keep the build working
# on older dpkg releases, but make the loss of root ownership visible.
DPKG_ROOT_FLAG="--root-owner-group"
if ! dpkg-deb --help 2>&1 | grep -q -- '--root-owner-group'; then
  warn "dpkg-deb does not support --root-owner-group; the package may not be root-owned"
  DPKG_ROOT_FLAG=""
fi
DPKG_VERSION=$(dpkg-deb --version 2>/dev/null | head -n 1 || true)
log "dpkg-deb version: ${DPKG_VERSION:-unknown}"

if [ ! -f "$APP_DIR/$EXE_NAME" ]; then
  die "app executable '$EXE_NAME' not found in $APP_DIR (wrong --app-dir/--exe-name?)"
fi
if [ ! -f "$APP_DIR/resources/app.asar" ]; then
  die "resources/app.asar not found in $APP_DIR (is this electron-builder's --linux dir output?)"
fi
if [ ! -d "$APP_DIR/resources/runtime" ]; then
  if [ "$ALLOW_MISSING_RUNTIME" -eq 1 ]; then
    warn "resources/runtime is missing; continuing because --allow-missing-runtime was given"
  else
    die "resources/runtime not found in $APP_DIR (use --allow-missing-runtime to override)"
  fi
fi
if [ ! -f "$APP_DIR/chrome-sandbox" ]; then
  if [ "$ALLOW_MISSING_SANDBOX" -eq 1 ]; then
    warn "chrome-sandbox is missing; continuing because --allow-missing-sandbox was given"
  else
    die "chrome-sandbox not found in $APP_DIR (use --allow-missing-sandbox to override)"
  fi
fi

# Pick the icon source. --icon wins; otherwise use a pre-sized icon. When no
# icon is given at all, fall back to the icon that electron-builder copies into
# resources/ from the desktop resources directory.
if [ -z "$ICON" ] && [ -z "$ICON_256" ] && [ -z "$ICON_512" ]; then
  if [ -f "$APP_DIR/resources/icon.png" ]; then
    ICON="$APP_DIR/resources/icon.png"
    log "auto-detected icon: $ICON"
  else
    die "no icon given and $APP_DIR/resources/icon.png does not exist (use --icon)"
  fi
fi
if [ -n "$ICON" ]; then
  [ -f "$ICON" ] || die "icon does not exist: $ICON"
  ICON_SOURCE=$ICON
elif [ -n "$ICON_512" ]; then
  [ -f "$ICON_512" ] || die "icon does not exist: $ICON_512"
  ICON_SOURCE=$ICON_512
else
  [ -f "$ICON_256" ] || die "icon does not exist: $ICON_256"
  ICON_SOURCE=$ICON_256
fi
log "icon source: $ICON_SOURCE"

DEPENDS=${DEPENDS_OVERRIDE:-$DEFAULT_DEPENDS}
RECOMMENDS=${RECOMMENDS_OVERRIDE:-$DEFAULT_RECOMMENDS}
require_clean_value "Depends" "$DEPENDS"
require_clean_value "Recommends" "$RECOMMENDS"

# ---------------------------------------------------------------------------
# staging directory
# ---------------------------------------------------------------------------

WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/deepseek-harness-deb.XXXXXX") || die "cannot create staging directory"
trap cleanup EXIT HUP INT TERM
STAGE="$WORKDIR/root"
APP_STAGE="$STAGE/opt/$PACKAGE_NAME"
DEBIAN_STAGE="$STAGE/DEBIAN"
log "staging directory: $WORKDIR"

mkdir -p \
  "$DEBIAN_STAGE" \
  "$APP_STAGE" \
  "$STAGE/usr/bin" \
  "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/256x256/apps" \
  "$STAGE/usr/share/icons/hicolor/512x512/apps" \
  "$STAGE/usr/share/doc/$PACKAGE_NAME"

# ---------------------------------------------------------------------------
# step 2: copy the application payload to /opt/<package>
# ---------------------------------------------------------------------------

log "[2/8] copying application payload into /opt/$PACKAGE_NAME"
cp -a "$APP_DIR/." "$APP_STAGE/" || die "failed to copy $APP_DIR into the staging tree"
[ -f "$APP_STAGE/$EXE_NAME" ] || die "staged executable '$EXE_NAME' is missing"
chmod 0755 "$APP_STAGE/$EXE_NAME" || die "cannot make '$EXE_NAME' executable"

if [ -e "$APP_STAGE/DEBIAN" ]; then
  die "the app directory itself contains a DEBIAN/ directory; refusing to mix control data"
fi

if [ -f "$APP_STAGE/chrome-sandbox" ]; then
  # dpkg-deb --root-owner-group supplies root:root; the setuid bit must be set
  # here because the build does not run as root. postinst re-applies it after
  # installation as a safety net.
  chmod 4755 "$APP_STAGE/chrome-sandbox" || die "cannot chmod 4755 chrome-sandbox"
  log "chrome-sandbox staged as root:root 4755 (setuid bit preserved in data.tar)"
fi

STAGED_FILES=$(find "$APP_STAGE" -type f | wc -l | tr -d ' ')
STAGED_SIZE=$(du -sh "$APP_STAGE" | cut -f1)
log "payload staged: $STAGED_FILES files, $STAGED_SIZE"

# ---------------------------------------------------------------------------
# step 3: launcher, desktop entry and icons
# ---------------------------------------------------------------------------

log "[3/8] installing launcher, desktop entry and icons"

PACKAGE_ESC=$(escape_sed_replacement "$PACKAGE_NAME")
EXE_ESC=$(escape_sed_replacement "$EXE_NAME")
APP_DIR_ESC=$(escape_sed_replacement "/opt/$PACKAGE_NAME")
MAINTAINER_ESC=$(escape_sed_replacement "$MAINTAINER")
HOMEPAGE_ESC=$(escape_sed_replacement "$HOMEPAGE")
VERSION_ESC=$(escape_sed_replacement "$VERSION")

sed \
  -e "s|@@PACKAGE_NAME@@|$PACKAGE_ESC|g" \
  -e "s|@@EXE_NAME@@|$EXE_ESC|g" \
  -e "s|@@APP_DIR@@|$APP_DIR_ESC|g" \
  "$SCRIPT_DIR/deepseek-harness.launcher" > "$STAGE/usr/bin/$PACKAGE_NAME"
chmod 0755 "$STAGE/usr/bin/$PACKAGE_NAME"
if grep -q '@@' "$STAGE/usr/bin/$PACKAGE_NAME"; then
  die "launcher still contains unsubstituted @@ placeholders"
fi

sed -e "s|@@PACKAGE_NAME@@|$PACKAGE_ESC|g" \
  "$SCRIPT_DIR/deepseek-harness.desktop" > "$STAGE/usr/share/applications/$PACKAGE_NAME.desktop"
chmod 0644 "$STAGE/usr/share/applications/$PACKAGE_NAME.desktop"
if grep -q '@@' "$STAGE/usr/share/applications/$PACKAGE_NAME.desktop"; then
  die "desktop entry still contains unsubstituted @@ placeholders"
fi

# The desktop package also ships the bundled `dsh` command line. Its launcher
# lives in the application payload; expose it on PATH with a package-owned
# symlink (dpkg removes it on uninstall, postrm has nothing to undo).
CLI_RELPATH="resources/runtime/cli/bin/dsh"
CLI_LAUNCHER="$APP_STAGE/$CLI_RELPATH"
CLI_TARGET="/opt/$PACKAGE_NAME/$CLI_RELPATH"
if [ ! -f "$CLI_LAUNCHER" ]; then
  die "bundled CLI launcher is missing: $CLI_RELPATH (the packaged runtime must provide 'dsh'; README/CI promise 'dsh --help')"
fi
chmod 0755 "$CLI_LAUNCHER" || die "cannot make the bundled dsh launcher executable"
ln -s "$CLI_TARGET" "$STAGE/usr/bin/dsh" || die "cannot create the /usr/bin/dsh symlink"
if [ -L "$STAGE/usr/bin/dsh" ]; then
  log "installed /usr/bin/dsh -> $CLI_TARGET"
else
  warn "staging filesystem did not create a real symlink for /usr/bin/dsh"
fi

resize_icon "$ICON_SOURCE" \
  "$STAGE/usr/share/icons/hicolor/256x256/apps/$PACKAGE_NAME.png" 256
resize_icon "$ICON_SOURCE" \
  "$STAGE/usr/share/icons/hicolor/512x512/apps/$PACKAGE_NAME.png" 512
chmod 0644 \
  "$STAGE/usr/share/icons/hicolor/256x256/apps/$PACKAGE_NAME.png" \
  "$STAGE/usr/share/icons/hicolor/512x512/apps/$PACKAGE_NAME.png"

# ---------------------------------------------------------------------------
# step 4: documentation
# ---------------------------------------------------------------------------

log "[4/8] generating /usr/share/doc/$PACKAGE_NAME"

sed -e "s|@@PACKAGE_NAME@@|$PACKAGE_ESC|g" \
    -e "s|@@HOMEPAGE@@|$HOMEPAGE_ESC|g" \
    "$SCRIPT_DIR/copyright" > "$STAGE/usr/share/doc/$PACKAGE_NAME/copyright"
chmod 0644 "$STAGE/usr/share/doc/$PACKAGE_NAME/copyright"

CHANGELOG_DATE=$(format_changelog_date "$EPOCH")
sed -e "s|@@PACKAGE_NAME@@|$PACKAGE_ESC|g" \
    -e "s|@@VERSION@@|$VERSION_ESC|g" \
    -e "s|@@MAINTAINER@@|$MAINTAINER_ESC|g" \
    -e "s|@@DATE@@|$CHANGELOG_DATE|g" \
    "$SCRIPT_DIR/changelog.Debian" > "$WORKDIR/changelog.Debian"
gzip -9 -n -c "$WORKDIR/changelog.Debian" > "$STAGE/usr/share/doc/$PACKAGE_NAME/changelog.Debian.gz"
chmod 0644 "$STAGE/usr/share/doc/$PACKAGE_NAME/changelog.Debian.gz"

# ---------------------------------------------------------------------------
# step 5: maintainer scripts
# ---------------------------------------------------------------------------

log "[5/8] installing maintainer scripts"
for script in postinst prerm postrm; do
  sed -e "s|@@PACKAGE_NAME@@|$PACKAGE_ESC|g" "$SCRIPT_DIR/$script" > "$DEBIAN_STAGE/$script"
  chmod 0755 "$DEBIAN_STAGE/$script"
  if grep -q '@@' "$DEBIAN_STAGE/$script"; then
    die "$script still contains unsubstituted @@ placeholders"
  fi
done
chmod 0755 "$DEBIAN_STAGE"

# ---------------------------------------------------------------------------
# step 6: control metadata incl. Installed-Size
# ---------------------------------------------------------------------------

log "[6/8] writing control metadata"

# du of the payload directories only: dpkg's Installed-Size excludes DEBIAN/.
INSTALLED_SIZE=$(du -sk "$STAGE/opt" "$STAGE/usr" | awk '{ total += $1 } END { print total }')
case "$INSTALLED_SIZE" in
  ''|*[!0-9]*) die "could not compute Installed-Size" ;;
esac
[ "$INSTALLED_SIZE" -gt 0 ] || die "computed Installed-Size is zero"

cat > "$DEBIAN_STAGE/control" <<EOF
Package: $PACKAGE_NAME
Version: $VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_SIZE
Depends: $DEPENDS
Recommends: $RECOMMENDS
Section: $SECTION
Priority: $PRIORITY
Homepage: $HOMEPAGE
Description: DeepSeek Harness desktop application
 DeepSeek Harness is the desktop shell for the DeepSeek Harness agent
 harness. This package bundles the Electron application, the dsh command
 line and the local Host runtime, and installs the application under
 /opt/$PACKAGE_NAME.
 .
 The desktop entry, hicolor icons and the /usr/bin/$PACKAGE_NAME launcher
 integrate the application with the Linux desktop.
EOF
chmod 0644 "$DEBIAN_STAGE/control"
log "control: Installed-Size=${INSTALLED_SIZE} KiB, $(printf '%s' "$DEPENDS" | tr ',' '\n' | wc -l | tr -d ' ') dependency groups"

# ---------------------------------------------------------------------------
# step 7: md5sums
# ---------------------------------------------------------------------------

log "[7/8] generating md5sums"
# Paths relative to the package root, NUL-separated, byte-sorted for stable output.
( cd "$STAGE" && find . -type f ! -path './DEBIAN/*' -print0 ) \
  | LC_ALL=C sort -z > "$WORKDIR/payload-files.list"
# --text keeps the canonical "digest  path" form on platforms whose md5sum
# defaults to binary mode (the Linux build is unaffected).
( cd "$STAGE" && xargs -0 md5sum --text < "$WORKDIR/payload-files.list" ) \
  > "$WORKDIR/md5sums.raw"
sed 's|^\([0-9a-f]\{32\}  \)\./|\1|' "$WORKDIR/md5sums.raw" \
  > "$DEBIAN_STAGE/md5sums"
chmod 0644 "$DEBIAN_STAGE/md5sums"

PAYLOAD_FILE_COUNT=$(tr -cd '\000' < "$WORKDIR/payload-files.list" | wc -c | tr -d ' ')
MD5_LINE_COUNT=$(wc -l < "$DEBIAN_STAGE/md5sums" | tr -d ' ')
[ "$PAYLOAD_FILE_COUNT" -eq "$MD5_LINE_COUNT" ] \
  || die "md5sums covers $MD5_LINE_COUNT of $PAYLOAD_FILE_COUNT payload files"
log "md5sums: $MD5_LINE_COUNT entries"

# ---------------------------------------------------------------------------
# step 8: deterministic mtimes, dpkg-deb build, summary
# ---------------------------------------------------------------------------

log "[8/8] normalising mtimes and building the .deb"

# Reproducibility: every file/directory (including DEBIAN/) gets the same mtime
# and dpkg-deb is told the same SOURCE_DATE_EPOCH, which also clamps members.
if ! find "$STAGE" -exec touch -h -d "@$EPOCH" {} + 2>/dev/null; then
  warn "touch -h -d is not supported here; member times will not be reproducible"
  find "$STAGE" -exec touch {} + 2>/dev/null || true
fi
export SOURCE_DATE_EPOCH="$EPOCH"
export TZ=UTC

if [ "$COMPRESSION" = "auto" ]; then
  if ! attempt_build zstd; then
    warn "zstd compression is unavailable; falling back to xz"
    attempt_build xz || die "dpkg-deb failed with both zstd and xz compression"
  fi
else
  attempt_build "$COMPRESSION" || die "dpkg-deb failed with $COMPRESSION compression"
fi

[ -f "$WORKDIR/package.deb" ] || die "dpkg-deb did not produce an archive"
mv "$WORKDIR/package.deb" "$DEB_FILE"
chmod 0644 "$DEB_FILE"

DEB_BYTES=$(wc -c < "$DEB_FILE" | tr -d ' ')
if command -v sha256sum >/dev/null 2>&1; then
  DEB_SHA256=$(sha256sum "$DEB_FILE" | cut -d' ' -f1)
else
  DEB_SHA256="unavailable"
fi

log "built $DEB_FILE"
log "  architecture: $ARCH"
log "  payload files: $PAYLOAD_FILE_COUNT"
log "  Installed-Size: ${INSTALLED_SIZE} KiB"
log "  compression: $ACTUAL_COMPRESSION"
log "  size: $DEB_BYTES bytes"
log "  sha256: $DEB_SHA256"

if [ "$RUN_VERIFY" -eq 1 ]; then
  log "running structural verification with verify_deb.py"
  "$PYTHON" "$TOOLS_DIR/verify_deb.py" \
    --arch "$ARCH" \
    --package-name "$PACKAGE_NAME" \
    --exe-name "$EXE_NAME" \
    --version "$VERSION" \
    "$DEB_FILE" || die "verify_deb.py rejected the produced package"
fi

printf 'DEB_BUILD_OK package=%s version=%s arch=%s compression=%s bytes=%s sha256=%s installed_size_kb=%s path=%s\n' \
  "$PACKAGE_NAME" "$VERSION" "$ARCH" "$ACTUAL_COMPRESSION" "$DEB_BYTES" "$DEB_SHA256" "$INSTALLED_SIZE" "$DEB_FILE"
