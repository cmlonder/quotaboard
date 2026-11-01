#!/usr/bin/env bash
# Put a `quotaboard` command on PATH (a symlink in ~/.local/bin) and check the optional tools.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="${QUOTABOARD_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN"
ln -sf "$HERE/bin/quotaboard" "$BIN/quotaboard"
echo "Installed: $BIN/quotaboard -> $HERE/bin/quotaboard"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not on your PATH; add it to your shell profile." ;; esac

command -v python3 >/dev/null || { echo "python3 is required."; exit 1; }
echo
echo "Optional tools (each one adds a section when present):"
for t in ccusage claude codex agy opencode multica; do
  if command -v "$t" >/dev/null; then echo "  ✓ $t"; else echo "  · $t not found"; fi
done
command -v ccusage >/dev/null || echo "  Claude spending needs ccusage: npm i -g ccusage"
CFG="$HOME/.config/quotaboard/config.json"
[[ -f "$CFG" ]] || echo "No config yet; it is optional. To add one: mkdir -p ~/.config/quotaboard && cp $HERE/config.example.json $CFG"
echo
echo "Run: quotaboard"
