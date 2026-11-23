#!/usr/bin/env bash
# Removes what install.sh set up. Keeps your settings, API key and dictation history unless --purge.
#
#   ./uninstall.sh             remove services, command, Python environment, hooks, desktop entries
#   ./uninstall.sh --purge     also delete ~/.config/dictate (API key, settings) and the history
#   ./uninstall.sh --dry-run   show what would happen, change nothing
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="$HOME/.local/share/dictate"
BIN="$HOME/.local/bin/dictate"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/dictate"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/dictate"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SETTINGS="$HOME/.claude/settings.json"

DRY=false PURGE=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=true ;;
    --purge) PURGE=true ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
run() { if $DRY; then printf '    would run:'; printf ' %q' "$@"; echo; else "$@"; fi; }
# remove_dir PATH: refuses anything that isn't one of ours under $HOME
remove_dir() {
  [[ -n $HOME && $1 == "$HOME"/* && $1 == */dictate && -d $1 ]] || return 0
  run rm -rf -- "$1"
}

step "Services"
for unit in dictate-tray.service dictate.service; do
  if [[ -e "$UNITS/$unit" ]]; then
    run systemctl --user disable --now "$unit" || true
    run rm -f -- "$UNITS/$unit"
  fi
done
run systemctl --user daemon-reload
note "Leaving ydotool.service running (other tools may use it)."

step "Command and Python environment"
if [[ -L "$BIN" && "$(readlink "$BIN")" == "$REPO/dictate.py" ]]; then run rm -f -- "$BIN"; fi
remove_dir "$DATA"
[[ -d "$REPO/clip" ]] && run make --no-print-directory -C "$REPO/clip" clean

step "Claude Code hooks"
if [[ -e "$SETTINGS" ]] && grep -q 'dictate hook' "$SETTINGS"; then
  if $DRY; then
    note "would remove the dictate hooks from $SETTINGS (with a backup)"
  else
    cp "$SETTINGS" "$SETTINGS.bak-dictate-uninstall"
    python3 -I - "$SETTINGS" <<'EOF'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); s = json.loads(p.read_text())
hooks = s.get("hooks", {})
for ev in list(hooks):
    kept = []
    for entry in hooks[ev]:
        entry["hooks"] = [h for h in entry.get("hooks", []) if "dictate hook" not in h.get("command", "")]
        if entry["hooks"]:
            kept.append(entry)
    if kept:
        hooks[ev] = kept
    else:
        del hooks[ev]
if not hooks:
    s.pop("hooks", None)
p.write_text(json.dumps(s, indent=2) + "\n")
EOF
    note "Removed. Backup: $SETTINGS.bak-dictate-uninstall"
  fi
else
  note "None found."
fi

step "Desktop entries and shortcuts"
for f in "$APPS"/claude-dictation*.desktop; do
  [[ -e $f ]] || continue
  id=$(basename "$f")
  run rm -f -- "$f"
  command -v kwriteconfig6 >/dev/null &&
    run kwriteconfig6 --file kglobalshortcutsrc --group services --group "$id" --key _launch --delete
done
command -v kbuildsycoca6 >/dev/null && run kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
other=$(grep -l -E '^Exec=dictate( |$)' "$APPS"/*.desktop 2>/dev/null || true)
[[ -n $other ]] && note "Shortcuts you created yourself still run 'dictate'; remove them in System Settings:" &&
  for f in $other; do note "  $(basename "$f")"; done

if $PURGE; then
  step "Settings, API key and history"
  if ! $DRY && [[ -t 0 ]]; then
    read -rp "    Delete $CONFIG and $STATE permanently? [y/N] " ans
    [[ $ans == [yY]* ]] || { note "Kept."; PURGE=false; }
  fi
  $PURGE && { remove_dir "$CONFIG"; remove_dir "$STATE"; }
else
  note "Kept $CONFIG (settings, API key) and $STATE (history). Use --purge to delete them."
fi

echo
echo "Claude Dictation is uninstalled. The repository itself is untouched."
