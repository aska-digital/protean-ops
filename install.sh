#!/usr/bin/env bash
# protean-ops - standalone installer.
# Usage: bash install.sh --target <dir> [--dry-run]
# Bash and coreutils only. No network access. Prints every path it writes.
set -euo pipefail

SLUG="protean-ops"
VERSION="1.0.0"

PAYLOAD=(
  "records"
  "scripts/protean-ops"
  "gates/protean-ops")

TARGETS=(
  "records"
  "scripts/protean-ops"
  "gates/protean-ops")

TARGET=""
DRY_RUN=0

usage() {
  printf 'usage: bash install.sh --target <dir> [--dry-run]\n'
  printf 'installs %s %s into <dir>; --dry-run prints the plan and writes nothing.\n' "$SLUG" "$VERSION"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --target) [ $# -ge 2 ] || { printf 'error: usage: --target needs a directory\n' >&2; exit 1; }; TARGET="$2"; shift 2 ;;
    --target=*) TARGET="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'error: usage: unknown flag: %s\n' "$1" >&2; usage >&2; exit 1 ;;
  esac
done

if [ -z "$TARGET" ]; then
  printf 'error: usage: --target <dir> is required\n' >&2
  usage >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for p in "${PAYLOAD[@]}"; do
  if [ ! -e "$ROOT/$p" ]; then
    printf 'error: install: declared payload path missing from the repository: %s\n' "$p" >&2
    exit 5
  fi
done

plan_files() {
  for p in "${PAYLOAD[@]}"; do
    if [ -d "$ROOT/$p" ]; then
      find "$ROOT/$p" -type f | sed "s#^$ROOT/##" | LC_ALL=C sort
    else
      printf '%s\n' "$p"
    fi
  done
}

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'installer: %s %s\n' "$SLUG" "$VERSION"
  printf 'mode: dry-run\n'
  printf 'target: %s\n' "$TARGET"
  printf 'network calls: 0\n'
  printf 'install targets:\n'
  for t in "${TARGETS[@]}"; do printf '  %s\n' "$t"; done
  printf 'planned writes (target-relative):\n'
  plan_files | sed "s#^#  $TARGET/#"
  printf 'result: dry-run, no files written\n'
  exit 0
fi

mkdir -p "$TARGET"
[ -w "$TARGET" ] || { printf 'error: install: target is not writable: %s\n' "$TARGET" >&2; exit 5; }

for p in "${PAYLOAD[@]}"; do
  dest="$TARGET/$p"
  mkdir -p "$(dirname "$dest")"
  if [ -d "$ROOT/$p" ]; then
    mkdir -p "$dest"
    cp -R "$ROOT/$p/." "$dest/"
  else
    cp "$ROOT/$p" "$dest"
  fi
done

plan_files | while IFS= read -r rel; do
  printf 'wrote: %s\n' "$TARGET/$rel"
done

printf 'installed: %s %s -> %s\n' "$SLUG" "$VERSION" "$TARGET"
printf 'result: ok, exit 0\n'
exit 0
