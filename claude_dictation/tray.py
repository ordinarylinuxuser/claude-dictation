"""KDE tray icon (StatusNotifierItem via AyatanaAppIndicator). Runs with the system python3."""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import ipc
from .config import (APP, DESKTOP_ID, HISTORY_FILE, MODES, RUNTIME, autostop_enabled, default_mode,
                     preview_enabled, set_autostop, set_mode, set_preview)
from .notify import describe
from .project import glossary_path

COLORS = {"off": "#7f8c8d", "idle": "#3d6a99", "recording": "#d63a3a", "processing": "#d98e04"}


def write_icons(folder: Path) -> None:
    """One SVG per state and mode: a coloured disc with the mode's initial; struck through when off."""
    folder.mkdir(parents=True, exist_ok=True)
    for state, color in COLORS.items():
        for mode in MODES:
            strike = '<line x1="4" y1="18" x2="18" y2="4" stroke="#ffffff" stroke-width="2.2"/>' if state == "off" else ""
            ring = ('<circle cx="11" cy="11" r="10" fill="none" stroke="#ffffff" stroke-opacity="0.9" '
                    'stroke-width="1.5" stroke-dasharray="3 2"/>') if state == "processing" else ""
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">'
                   f'<circle cx="11" cy="11" r="10" fill="{color}"/>{ring}'
                   f'<text x="11" y="15.5" font-family="sans-serif" font-size="13" font-weight="700" '
                   f'text-anchor="middle" fill="#ffffff">{mode[0].upper()}</text>{strike}</svg>')
            (folder / f"dictate-{state}-{mode}.svg").write_text(svg)


def _open(path: Path) -> None:
    subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def headline(st: dict | None) -> str:
    """What the tray says about the current state (also its tooltip)."""
    if st is None:
        return "Dictation service is not running"
    line = describe(st["mode"], st["preview"], Path(st["project"]) if st["project"] else None,
                    st["state"] == "recording" and st.get("autostop", False))
    more = st.get("pending", 0)
    if st["state"] == "recording":
        return f"Recording {st['seconds']} s · {line}" + (f" · {more} earlier in progress" if more else "")
    if st["state"] == "processing":
        return f"Working: {st.get('stage_text') or 'processing'}" + (f" (+{more - 1} queued)" if more > 1 else "")
    return f"Ready · {line}"


def run() -> int:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AI, GLib, Gtk

    icons = RUNTIME / "dictate-icons"
    write_icons(icons)
    ind = AI.Indicator.new(DESKTOP_ID, "dictate-off-clean", AI.IndicatorCategory.APPLICATION_STATUS)
    ind.set_icon_theme_path(str(icons))
    ind.set_status(AI.IndicatorStatus.ACTIVE)
    ind.set_title("Claude Dictation")

    menu = Gtk.Menu()
    header = Gtk.MenuItem(label="Starting…")
    header.set_sensitive(False)
    toggle = Gtk.MenuItem(label="Start dictation")
    cancel = Gtk.MenuItem(label="Cancel recording")
    repaste = Gtk.MenuItem(label="Paste last dictation again")
    mode_items: dict[str, Gtk.RadioMenuItem] = {}
    group = None
    for m in MODES:
        item = Gtk.RadioMenuItem.new_with_label_from_widget(group, f"{m.capitalize()} mode")
        group = group or item
        mode_items[m] = item
    preview_item = Gtk.CheckMenuItem(label="Show preview before pasting")
    autostop_item = Gtk.CheckMenuItem(label="Stop automatically after a pause")
    history_item = Gtk.MenuItem(label="Open dictation history")
    glossary_item = Gtk.MenuItem(label="Edit project glossary")
    restart_item = Gtk.MenuItem(label="Restart dictation service")
    quit_item = Gtk.MenuItem(label="Hide tray icon")
    for w in (header, Gtk.SeparatorMenuItem(), toggle, cancel, repaste, Gtk.SeparatorMenuItem(),
              *mode_items.values(), preview_item, autostop_item, Gtk.SeparatorMenuItem(),
              history_item, glossary_item, restart_item, Gtk.SeparatorMenuItem(), quit_item):
        menu.append(w)
    menu.show_all()
    ind.set_menu(menu)
    ind.set_secondary_activate_target(toggle)  # middle-click starts or stops

    updating = False  # set while poll() changes widgets, so their signals don't echo back

    def on_mode(item, m: str) -> None:
        if not updating and item.get_active() and m != default_mode():
            set_mode(m)
            ipc.notice("setting", f"Default mode: {m}.")

    def on_switch(item, read, write, on_text: str, off_text: str) -> None:
        if not updating and item.get_active() != read():
            on = write("on" if item.get_active() else "off")
            ipc.notice("setting", on_text if on else off_text)

    toggle.connect("activate", lambda *_: ipc.request({"cmd": "toggle"}))
    cancel.connect("activate", lambda *_: ipc.request({"cmd": "cancel"}))
    repaste.connect("activate", lambda *_: ipc.request({"cmd": "repaste"}))
    for m, item in mode_items.items():
        item.connect("toggled", on_mode, m)
    preview_item.connect("toggled", on_switch, preview_enabled, set_preview,
                         "Preview on: you review each dictation.", "Preview off: dictations paste directly.")
    autostop_item.connect("toggled", on_switch, autostop_enabled, set_autostop,
                          "Auto-stop on: recording ends after a pause.", "Auto-stop off: press the hotkey to stop.")
    history_item.connect("activate", lambda *_: HISTORY_FILE.exists() and _open(HISTORY_FILE))
    glossary_item.connect("activate", lambda *_: _open(glossary_path()))
    restart_item.connect("activate", lambda *_: subprocess.Popen(["systemctl", "--user", "restart", APP]))
    quit_item.connect("activate", lambda *_: Gtk.main_quit())

    def poll() -> bool:
        nonlocal updating
        st = ipc.status()
        state = "off" if st is None else st["state"]
        mode = st["mode"] if st else default_mode()
        updating = True
        try:
            header.set_label(headline(st))
            toggle.set_label("Stop and paste" if state == "recording" else "Start dictation")
            toggle.set_sensitive(st is not None)
            cancel.set_sensitive(state == "recording")
            repaste.set_sensitive(st is not None)
            ind.set_icon_full(f"dictate-{state}-{mode}", header.get_label())
            ind.set_title(f"Claude Dictation: {header.get_label()}")
            dm = default_mode()
            if not mode_items[dm].get_active():
                mode_items[dm].set_active(True)
            for item, read in ((preview_item, preview_enabled), (autostop_item, autostop_enabled)):
                if item.get_active() != read():
                    item.set_active(read())
        finally:
            updating = False
        return True

    poll()
    GLib.timeout_add(500, poll)
    Gtk.main()
    return 0
