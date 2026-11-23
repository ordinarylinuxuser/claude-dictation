#!/usr/bin/env bash
# Installs Claude Dictation for the current user. Safe to run again: each step checks first.
#
#   ./install.sh                 install or update
#   ./install.sh --dry-run       show what would happen, change nothing
#   ./install.sh --no-hooks      don't add the Claude Code project-tracking hooks
#   ./install.sh --no-tray       don't install the tray icon
#   ./install.sh --no-shortcuts  don't add KDE keyboard shortcuts
#
# Shortcut keys can be chosen with environment variables, e.g.
#   DICTATE_KEY_TOGGLE="Meta+Num+Ins" ./install.sh
# DICTATE_KEY_TOGGLE (Meta+Alt+Space), DICTATE_KEY_STRUCTURE (Meta+Alt+S), DICTATE_KEY_CANCEL (Meta+Alt+X),
# DICTATE_KEY_MODE (Meta+Alt+M), DICTATE_KEY_REPASTE (Meta+Alt+V). Set one to "none" to skip it.
#
# Removal: ./uninstall.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/.local/share/dictate/venv"
BIN="$HOME/.local/bin"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/dictate"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SETTINGS="$HOME/.claude/settings.json"

DRY=false HOOKS=true TRAY=true SHORTCUTS=true
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=true ;;
    --no-hooks) HOOKS=false ;;
    --no-tray) TRAY=false ;;
    --no-shortcuts) SHORTCUTS=false ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
run() { if $DRY; then printf '    would run:'; printf ' %q' "$@"; echo; else "$@"; fi; }
# write_file PATH MODE: content on stdin
write_file() {
  if $DRY; then note "would write $1"; cat >/dev/null; return; fi
  mkdir -p "$(dirname "$1")"; cat >"$1"; chmod "$2" "$1"
}

step "Checking dependencies"
missing=()
need() { command -v "$1" >/dev/null 2>&1 || missing+=("$2"); }
need python3 python;          need pw-record pipewire;     need wl-copy wl-clipboard
need notify-send libnotify;   need kdialog kdialog;        need ydotool ydotool
need gcc gcc;                 need wayland-scanner wayland; need pkg-config pkgconf
need xdg-open xdg-utils
if $TRAY && ! python3 -c 'import gi; gi.require_version("AyatanaAppIndicator3", "0.1")' 2>/dev/null; then
  missing+=(python-gobject libayatana-appindicator)
fi
if [[ ! -e /usr/share/wayland-protocols/staging/ext-data-control/ext-data-control-v1.xml &&
      ! -e /usr/share/qt6/wayland/protocols/wlr-data-control/wlr-data-control-unstable-v1.xml ]]; then
  missing+=(wayland-protocols)
fi
if ((${#missing[@]})); then
  mapfile -t missing < <(printf '%s\n' "${missing[@]}" | sort -u)
  note "Missing packages: ${missing[*]}"
  if $DRY; then
    note "would offer: sudo pacman -S --needed ${missing[*]}"
  elif [[ -t 0 ]] && read -rp "    Install them now with sudo pacman? [y/N] " ans && [[ $ans == [yY]* ]]; then
    sudo pacman -S --needed "${missing[@]}"
  else
    note "Install them with: sudo pacman -S --needed ${missing[*]}"; exit 1
  fi
else
  note "All required packages are installed."
fi
command -v kdotool >/dev/null || note "Optional: 'yay -S kdotool' picks Ctrl+V or Ctrl+Shift+V per window."

step "Python environment ($VENV)"
[[ -x "$VENV/bin/python" ]] || run python3 -m venv "$VENV"
run "$VENV/bin/pip" install --quiet --upgrade pip anthropic faster-whisper numpy

step "Building the rich-paste clipboard helper"
run make --no-print-directory -C "$REPO/clip"

step "Command: $BIN/dictate"
run mkdir -p "$BIN"
run ln -sfn "$REPO/dictate.py" "$BIN/dictate"
case ":$PATH:" in *":$BIN:"*) ;; *) note "Add $BIN to your PATH so 'dictate' works in a terminal." ;; esac

step "Configuration ($CONFIG)"
run mkdir -p "$CONFIG"
if [[ ! -e "$CONFIG/env" ]]; then
  write_file "$CONFIG/env" 600 <<'EOF'
# Claude Dictation settings (DICTATE_*). Uncomment to change; then: systemctl --user restart dictate
#DICTATE_WHISPER_MODEL=small.en        # large-v3-turbo: more accurate, ~3x slower
#DICTATE_NOTIFY=all                    # all | results (the tray shows progress) | errors | none
#DICTATE_STRUCTURE_EFFORT=medium       # off: faster structure mode, may drop details
#DICTATE_PASTE_KEYS=auto               # auto | ctrl+v | ctrl+shift+v
#DICTATE_PASTE_KEYS_FALLBACK=ctrl+shift+v
#DICTATE_RICH_PASTE=1                  # 0: plain-text clipboard only
#DICTATE_MAX_SECONDS=180
#DICTATE_AUTOSTOP_SECONDS=1.5          # pause that ends a recording (switch: dictate autostop on|off)
#DICTATE_FOCUS=restore                 # restore | warn | off: if you switched windows before the paste
EOF
else
  note "Keeping existing $CONFIG/env"
fi
if [[ -s "$CONFIG/anthropic_key" || -n "${DICTATE_ANTHROPIC_API_KEY:-}" ]]; then
  note "Anthropic API key already configured."
elif $DRY; then
  note "would ask for an Anthropic API key"
elif [[ -t 0 ]]; then
  read -rsp "    Anthropic API key for the cleanup step (Enter to skip): " key; echo
  if [[ -n $key ]]; then (umask 077; printf '%s\n' "$key" >"$CONFIG/anthropic_key"); note "Saved."; fi
  unset key
fi
[[ -s "$CONFIG/anthropic_key" ]] || $DRY || note "No API key yet: dictations will paste the raw transcript. Add one later with:
      install -Dm600 /dev/stdin $CONFIG/anthropic_key"

step "Desktop entry"
run mkdir -p "$APPS"
run install -m644 "$REPO/config/claude-dictation.desktop" "$APPS/claude-dictation.desktop"

step "Services"
install_unit() {
  sed "s|@REPO@|$REPO|g" "$REPO/config/$1" | write_file "$UNITS/$1" 644
}
install_unit dictate.service
$TRAY && install_unit dictate-tray.service
run systemctl --user daemon-reload
run systemctl --user enable --now ydotool.service
run systemctl --user enable dictate.service
run systemctl --user restart dictate.service
if $TRAY; then
  run systemctl --user enable dictate-tray.service
  run systemctl --user restart dictate-tray.service
fi

if $HOOKS; then
  step "Claude Code hooks (track which project you're in)"
  if [[ -e "$SETTINGS" ]] && grep -q 'dictate hook' "$SETTINGS"; then
    note "Already present."
  elif $DRY; then
    note "would add SessionStart and UserPromptSubmit hooks to $SETTINGS (with a backup)"
  else
    mkdir -p "$(dirname "$SETTINGS")"
    [[ -e "$SETTINGS" ]] && cp "$SETTINGS" "$SETTINGS.bak-dictate"
    python3 -I - "$SETTINGS" <<'EOF'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
s = json.loads(p.read_text()) if p.exists() and p.read_text().strip() else {}
entry = {"hooks": [{"type": "command", "command": "$HOME/.local/bin/dictate hook", "timeout": 5}]}
for ev in ("SessionStart", "UserPromptSubmit"):
    s.setdefault("hooks", {}).setdefault(ev, []).append(entry)
p.write_text(json.dumps(s, indent=2) + "\n")
EOF
    note "Added. Backup: $SETTINGS.bak-dictate"
  fi
fi

if $SHORTCUTS; then
  step "KDE keyboard shortcuts"
  existing=$(grep -l -E '^Exec=dictate( |$)' "$APPS"/*.desktop 2>/dev/null | grep -v '/claude-dictation.desktop$' || true)
  if [[ -n $existing ]]; then
    note "You already have dictate shortcuts; leaving them as they are:"
    for f in $existing; do note "  $(basename "$f"): $(sed -n 's/^Exec=//p' "$f")"; done
  elif ! command -v kwriteconfig6 >/dev/null; then
    note "kwriteconfig6 not found; add shortcuts in System Settings > Keyboard > Shortcuts."
  else
    added=0
    # file id | command | key
    while IFS='|' read -r id cmd key; do
      [[ $key == none ]] && continue
      if grep -q "=$key\(,\|$\)" "${XDG_CONFIG_HOME:-$HOME/.config}/kglobalshortcutsrc" 2>/dev/null; then
        note "Skipping $key for '$cmd': already used by another shortcut."; continue
      fi
      write_file "$APPS/$id" 644 <<EOF
[Desktop Entry]
Exec=$cmd
Name=$cmd
NoDisplay=true
StartupNotify=false
Type=Application
X-KDE-GlobalAccel-CommandShortcut=true
EOF
      run kwriteconfig6 --file kglobalshortcutsrc --group services --group "$id" --key _launch "$key"
      note "$key  ->  $cmd"; added=$((added + 1))
    done <<EOF
claude-dictation-toggle.desktop|dictate toggle|${DICTATE_KEY_TOGGLE:-Meta+Alt+Space}
claude-dictation-structure.desktop|dictate toggle --mode structure|${DICTATE_KEY_STRUCTURE:-Meta+Alt+S}
claude-dictation-cancel.desktop|dictate cancel|${DICTATE_KEY_CANCEL:-Meta+Alt+X}
claude-dictation-mode.desktop|dictate mode next|${DICTATE_KEY_MODE:-Meta+Alt+M}
claude-dictation-repaste.desktop|dictate repaste|${DICTATE_KEY_REPASTE:-Meta+Alt+V}
EOF
    if ((added)); then
      run kbuildsycoca6 --noincremental
      note "KDE loads new command shortcuts at login: log out and back in to use them."
    fi
  fi
fi

step "Checking the installation"
if $DRY; then note "would run: dictate doctor"; else sleep 3; "$REPO/dictate.py" doctor || true; fi
echo
echo "Done. Press your dictation shortcut, speak, and press it again."
