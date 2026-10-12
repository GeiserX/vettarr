#!/bin/sh
# Compile src/vettarr/web/static/src/app.css into static/app.css with the
# Tailwind standalone CLI. No Node: the CLI is one binary, downloaded once into
# .cache/tailwind (or $TAILWIND_CACHE) and checked against the sha256 below on
# every run. The Dockerfile's builder stage runs this same script.
#
#   scripts/build-css.sh            build once, minified
#   scripts/build-css.sh --watch    rebuild on every change, for UI work
#
# To move to another Tailwind release, change VERSION and the four sums (the
# release's sha256sums.txt lists them).
set -eu

VERSION=4.3.3
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)               ASSET=tailwindcss-linux-x64;   SUM=dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a ;;
  Linux-aarch64|Linux-arm64)  ASSET=tailwindcss-linux-arm64; SUM=55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195 ;;
  Darwin-arm64)               ASSET=tailwindcss-macos-arm64; SUM=cdf646702987a743464dff4d9c60fd4480d1c1e73dd819a9a67f1078815dce9d ;;
  Darwin-x86_64)              ASSET=tailwindcss-macos-x64;   SUM=7922e0953f2110c05976e3bf58f14e643d90427575e766b7d433f5f80cbee7e1 ;;
  *) echo "build-css: no Tailwind binary pinned for $(uname -s) $(uname -m)" >&2; exit 1 ;;
esac

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STATIC="$ROOT/src/vettarr/web/static"
CACHE=${TAILWIND_CACHE:-"$ROOT/.cache/tailwind"}
BIN="$CACHE/$ASSET-$VERSION"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

if [ ! -f "$BIN" ]; then
  mkdir -p "$CACHE"
  curl -fsSL -o "$BIN.part" "https://github.com/tailwindlabs/tailwindcss/releases/download/v$VERSION/$ASSET"
  mv -f "$BIN.part" "$BIN"
fi
GOT=$(sha256_of "$BIN")
if [ "$GOT" != "$SUM" ]; then
  rm -f "$BIN"
  echo "build-css: $ASSET $VERSION has sha256 $GOT, expected $SUM. The download was removed; nothing was built." >&2
  exit 1
fi
chmod +x "$BIN"

cd "$ROOT"
exec "$BIN" --input "$STATIC/src/app.css" --output "$STATIC/app.css" --minify "$@"
