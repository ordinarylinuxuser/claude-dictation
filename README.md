# claude-dictation

Speak your Claude Code prompts. Push a hotkey, talk, push it again. Local Whisper transcribes the audio, Claude Haiku turns it into the prompt you meant (using the project's glossary and `CLAUDE.md`), and it is pasted into Claude, either straight away or after you check it in an optional preview.

```
HOST (Arch/KDE)                                   VM (Arch, KDE 6.8 Wayland)          
mic → EasyEffects → processed source              ICH9 HDA input ("Built-in Audio")
                     └→ loopback "vm_mic" ─SPICE──→ │   (virt-viewer's recording stream)
                        (own mute, stable name)      ▼
                                                  dictate daemon  (hotkey: dictate toggle)
                                                   ├ pw-record 16 kHz mono
                                                   ├ faster-whisper small.en, CPU int8  (~0.9 s)
                                                   ├ Claude Haiku 5.5 cleanup + project context
                                                   ├ kdialog preview / edit
                                                   └ wl-copy + Ctrl+Shift+V  (or kwin-type)
                                                  Claude Code hook → "current project" file
```

## What I found on the VM

These were checked in this VM, not assumed:

| | |
|---|---|
| Hypervisor | QEMU/KVM, Q35 machine `pc-q35-11.1`, virtio devices, virtiofs share, emulated **ICH9 HDA** sound card (the VM already has an input device) |
| Session | KDE Plasma 6.8.3, **Wayland** (KWin 6.8.3). Claude desktop runs native Wayland; Chrome runs on XWayland |
| CPU / RAM | 8 vCPUs of a Ryzen 7 7800X3D, 21 GiB |
| GPU | Passed-through Quadro K620 (2 GB, Maxwell) on driver 580. Not useful for Whisper: no CUDA 12 runtime installed, CUDA 13 dropped Maxwell, and `large-v3-turbo` runs out of memory on 2 GB |
| Input injection | `wtype` **does not work on KWin** (no virtual-keyboard protocol). `/dev/uinput` already has an ACL for your user, so `ydotool` works without root. `kwin-type` (AUR) works on KWin ≥ 6.8 |
| Tools present | `pw-record`, `wl-copy`/`wl-paste`, `kdialog`, `notify-send`, Python 3.14, Claude Code 2.1.316 |

## Recommended architecture: route the audio into the VM, run everything else in the VM

**Why in the VM:** everything the cleanup step needs is in the VM: the project's `CLAUDE.md` and glossary, the git branch, which Claude Code session you are using, and the window you are typing into. On the host you would need a second channel to get that context out of the VM and a way to get text back in. The extra latency is small: the audio hop adds tens of milliseconds, and Whisper takes about 0.9 s on the VM's CPU.

**Why a separate `vm_mic` source on the host:** your idea of a second virtual device is worth doing. It gives the VM its own mute (`wpctl set-mute`) that does not touch your call mic. It also gives the VM a stable name that keeps working when EasyEffects restarts or renames nodes, and you can point it at the raw mic later without editing the VM. For the microphone direction, a loopback that creates a *source* is cleaner than a null sink plus its monitor. See [config/vm-mic.service](config/vm-mic.service).

### Alternative: transcribe on the host, send only text into the VM

| | Audio → VM (recommended) | Text only → VM |
|---|---|---|
| Audio setup | One-time: loopback + libvirt `<audio>` (or SPICE stream routing) | None |
| Project context | Read directly in the VM | Needs a channel out of the VM (virtiofs, ssh) |
| Delivery into Claude | Clipboard/paste inside the VM | Host must inject keys into the viewer window (layout and focus problems) or send text to an agent in the VM anyway |
| Hotkey | Inside the VM: fires while you work in the VM | Host hotkeys may not fire while a SPICE viewer has grabbed the keyboard, and never fire if the VM has its own keyboard |
| STT speed | VM CPU: small.en 0.9 s, turbo 2.7 s | Faster only if the host has a strong GPU |
| Claude Code `/voice` | Works (the VM has a "local" mic) | Not possible |

Host-side only wins if your host has a strong GPU and you want `large-v3` accuracy in about 0.3 s. Even then, the better hybrid is to keep the audio and the daemon in the VM and point Whisper at a host-side GPU server over the VM network. Moving STT is a config change, not a rewrite.

## Speech-to-text options

Measured on this VM: 11 s clip, warm model, `beam_size=1`, int8, 8 threads:

| Option | Latency | Accuracy on jargon | Setup | Notes |
|---|---|---|---|---|
| **faster-whisper `small.en`** (default) | **0.89 s** | Good with glossary hints | pip install | Model load 0.9 s once, held by the daemon |
| faster-whisper `large-v3-turbo` | 2.70 s | Best local | same | Set `DICTATE_WHISPER_MODEL=large-v3-turbo` if small.en mishears too often |
| faster-whisper `distil-large-v3.5` | 2.62 s | Very good, English | same | No faster than turbo here |
| whisper.cpp (AUR `whisper.cpp-git`, Vulkan) | similar on CPU | same models | build from AUR | The K620 is too weak to help. No reason to switch |
| Parakeet TDT 0.6B (sherpa-onnx, hyprwhspr) | reported much faster than Whisper on CPU | Excellent English, weaker vocabulary biasing | more moving parts | Worth trying later if 0.9 s feels slow |
| nerd-dictation (Vosk) | real-time | Noticeably worse | easy | Not recommended |
| voxtype / hyprwhspr (AUR) | — | Whisper/Parakeet | easy | Complete push-to-talk tools, but no Haiku step or project context, and they assume `wtype` or `ydotool` |
| **Claude Code `/voice`** | live, streaming | Tuned for code; adds project name and branch as hints | none | Built into the CLI, uses your claude.ai login, no token cost. No rewrite step and no glossary. Good for short replies, and the quickest way to test the VM mic |
| Cloud STT (OpenAI, Groq, Deepgram, ElevenLabs…) | ~0.3–1 s | Best, with keyterm prompts | API key | Audio leaves the machine and costs per minute. Local is fast enough here |

## Cleanup with Claude Haiku

- Model `claude-haiku-5-5`, with `thinking: {type: "disabled"}` and `effort: "low"` for the lowest latency. At $0.10 / $0.50 per million tokens, one dictation with full context costs a small fraction of a cent.
- The system prompt is in [claude_dictation/prompts/cleanup.md](claude_dictation/prompts/cleanup.md). Its main rule is that Haiku must **never answer** the dictation, because a spoken question is otherwise the classic failure mode. It also removes filler and false starts, applies "no wait, …" corrections, converts spoken code forms (`dash dash dry run` → `--dry-run`), corrects mis-hearings only toward glossary terms that sound similar, and never adds requirements.
- A `structure` mode (separate hotkey) lays longer dictations out at a glance: a short lead, numbered steps if you gave an order, bullets with a bold name and sub-bullets, and your closing ask. Haiku returns a fixed JSON shape and the script renders it, so the layout is guaranteed. Structure mode thinks at medium effort (about 4–6 s) because without thinking it dropped details on long dictations. `DICTATE_STRUCTURE_EFFORT=off` makes it about 2.5 s. A `raw` mode skips Haiku.
- Safety nets: if the API fails, refuses, or has no key, you get the raw transcript with a warning in the preview. If the cleanup is much longer than what you said, the preview warns that Haiku may have answered instead of rewriting.

### Project context

- **Which project:** a Claude Code `SessionStart` + `UserPromptSubmit` hook ([config/claude-hooks.json](config/claude-hooks.json)) writes the project directory of the session you last used to `~/.local/state/dictate/current-project.json`. The hotkey process has no useful working directory, and this follows you as you switch sessions. The hook prints nothing, which matters because hook output would become Claude context. Override with `dictate toggle -p DIR`.
- **What gets sent:** project name, git branch, `.claude/dictation.md` (or `.dictation-glossary.md`) up to 6 KB, `CLAUDE.md` up to 8 KB, top-level file names, and an optional personal `~/.config/dictate/glossary.md`.
- Glossary terms are also passed to Whisper as `initial_prompt`, so names are recognised correctly in the first place, not only fixed afterwards. Template: [examples/dictation.md](examples/dictation.md).

## Delivery into the Claude prompt

| Method | Works here? | Notes |
|---|---|---|
| **Rich clipboard + paste via ydotool** (default `paste`) | Yes | The clipboard offers HTML and Markdown together, through `clip/wl-copy-multi` (build: `make -C clip`). The Claude desktop box takes the HTML, so you get real bullets, numbering and bold. Terminals take the Markdown, with multi-line kept by bracketed paste. The paste key follows the focused window when `kdotool` (AUR) is installed: Ctrl+V in apps, Ctrl+Shift+V in terminals. Without it, `DICTATE_PASTE_KEYS_FALLBACK` is used (default Ctrl+Shift+V, which pastes plain text in the desktop app). Without ydotool it copies and tells you to paste |
| `kwin-type` (AUR, `DICTATE_DELIVERY=type`) | Yes (KWin 6.8.3) | Types through KWin's fake-input/libei, layout-aware. Newlines become spaces, because Enter submits in Claude Code |
| `ydotool type` | Yes | Fallback for `type`. Sends US-layout keycodes |
| `wtype` | **No** | KWin lacks the virtual-keyboard protocol |
| `xdotool` | No for Claude | Only reaches XWayland windows; Claude desktop is native Wayland |
| A direct hook into Claude Code | No | Hooks can add context or block a prompt but cannot fill the input box. The project-tracking hook above is the useful part |

The preview is a `kdialog` text box with the cleaned prompt, which you can edit, and the raw transcript beneath it. OK delivers the prompt, Cancel discards it. Nothing is submitted. You still press Enter in Claude yourself.

The preview is optional. With it off, the cleaned text is pasted straight into the Claude Code prompt, and you review and edit it there before pressing Enter. Any warning (cleanup skipped, a suspiciously long rewrite) then shows as a notification instead.

| Command | Effect |
|---|---|
| `dictate preview off` / `on` / `toggle` | Sets the default. It persists across restarts and takes effect from the next dictation |
| `dictate preview` | Shows the current setting |
| `dictate toggle --no-preview` / `--preview` | Overrides the default for one dictation, for example on a second hotkey |

## Setup guide

### 1. Host: give the VM the EasyEffects mic

If your VM runs under libvirt/virt-manager and you view it through virt-viewer or virt-manager (SPICE), **option A below is the one to use**. It needs no VM XML change and no change to the user QEMU runs as.

1. Check the EasyEffects processed source. On your host it is `easyeffects_source` ("Easy Effects Source", `Audio/Source/Virtual`). The `ee_sie_*` nodes are EasyEffects' meters and spectrum, not sources.
   ```bash
   pactl list short sources
   ```
2. **On the host**, install [config/vm-mic.service](config/vm-mic.service) as `~/.config/systemd/user/vm-mic.service`. The repo lives in the VM, so copy its contents across. Then:
   ```bash
   systemctl --user daemon-reload && systemctl --user enable --now vm-mic
   ```
   Why a service and not a `pipewire.conf.d` drop-in: at PipeWire startup `easyeffects_source` doesn't exist yet, so a drop-in loopback can't find its target and unloads itself without logging anything. The service retries every 3 s until EasyEffects is up. If you restart PipeWire, EasyEffects has to be restarted too, because it loses its nodes.
   Check that `vm_mic` exists and is fed by EasyEffects:
   ```bash
   pactl list short sources | grep vm_mic; pw-link -l | grep -B1 -A1 vm_mic
   ```
3. Connect it to the VM:
   - **A. SPICE recording stream (recommended for virt-viewer):** with the VM open in virt-viewer, open the host's KDE audio applet → Applications → recording streams, and set virt-viewer's stream to **VM Mic (EasyEffects)**. WirePlumber remembers the choice per application. The VM hears the mic only while virt-viewer is open, which is always the case for you. To confirm the VM uses SPICE audio (no `<audio>` element, or `<audio type='spice'>`):
     ```bash
     sudo virsh -c qemu:///system dumpxml <vm-name> | grep -E -A3 '<sound|<audio'
     ```
     The stream only appears once the VM opens its mic, so start a recording in the VM first (step 2 below).
     If it shows `<audio id='1' type='none'/>`, QEMU discards all VM audio, the VM records pure digital silence, and virt-viewer never shows a recording stream. Change it with `sudo virsh edit <vm-name>` to `<audio id='1' type='spice'/>`, then shut the VM down from the host and start it again. A reboot inside the guest is not enough. VM sound output will then also play on the host through virt-viewer.
   - **B. PipeWire backend (libvirt ≥ 9.10):** add [config/libvirt-audio.xml](config/libvirt-audio.xml) with `virsh edit <vm-name>`. Under `qemu:///system` QEMU runs as a separate user and can't reach your PipeWire socket, so you would also have to set `user`/`group` in `/etc/libvirt/qemu.conf`. Only worth it if you want VM audio without the viewer.
   - **C. Plain QEMU command line:** `-audiodev pipewire,id=snd0,in.name=vm_mic,in.latency=20000 -device ich9-intel-hda -device hda-duplex,audiodev=snd0`

### 2. VM: check the mic

```bash
pw-record --rate 16000 --channels 1 /tmp/mic-test.wav
```
Speak, stop with Ctrl+C, then play it back:
```bash
pw-play /tmp/mic-test.wav
```
Or run `/voice` in the Claude Code CLI and hold Space. If the level is low, raise "Built-in Audio" input in the VM's audio applet.

### 3. VM: install (or update)

```bash
~/Documents/gits/claude-dictation/install.sh
```

The installer is safe to run again, for example after pulling changes. It:
- **Checks packages** and offers to install any that are missing with `sudo pacman`.
- **Sets up the Python environment** in `~/.local/share/dictate/venv`, with anthropic and faster-whisper.
- **Builds the rich-paste clipboard helper** (`clip/wl-copy-multi`).
- **Links the `dictate` command** into `~/.local/bin`.
- **Asks for your Anthropic API key**, stored only for dictation in `~/.config/dictate/anthropic_key`. Don't export `ANTHROPIC_API_KEY` in your shell: Claude Code would then bill that key instead of your claude.ai plan, and `/voice` would stop working.
- **Writes a commented settings file**, `~/.config/dictate/env`.
- **Installs and starts the user services**: `dictate` (the daemon), `dictate-tray` (the tray icon) and `ydotool`.
- **Adds the Claude Code hooks** that track which project you're in, after backing up `~/.claude/settings.json`.
- **Adds KDE shortcuts**, unless you already have shortcuts that run `dictate`. KDE loads new command shortcuts at login.
- **Runs `dictate doctor`** to check the result.

Options: `--dry-run` shows the steps without changing anything, and `--no-hooks`, `--no-tray` and `--no-shortcuts` skip those parts.

Optional extras:
- `yay -S kdotool` picks Ctrl+V or Ctrl+Shift+V depending on the focused window.
- `yay -S kwin-type` lets `DICTATE_DELIVERY=type` type the text instead of pasting it.

### 4. VM: everyday use

| Default shortcut | Command | What it does |
|---|---|---|
| Meta+Alt+Space | `dictate toggle` | Starts and stops a dictation in the default mode |
| Meta+Alt+S | `dictate toggle --mode structure` | Starts and stops a dictation in structure mode |
| Meta+Alt+X | `dictate cancel` | Throws away the current recording |
| Meta+Alt+M | `dictate mode next` | Cycles the default mode: clean, structure, raw |
| Meta+Alt+V | `dictate repaste` | Pastes the last dictation again into the focused window |

Choose other keys at install time, for example `DICTATE_KEY_TOGGLE="Meta+Num+Ins" ./install.sh`. The variables are listed at the top of `install.sh`.

**How a dictation flows:**
- **Auto-stop.** The recording ends by itself after a 1.5 s pause once you've started talking, so one key press is enough. Switch it with `dictate autostop off`, or from the tray. Change the pause with `DICTATE_AUTOSTOP_SECONDS`.
- **Keep going.** You can start the next dictation while the last one is still being transcribed or tidied up. They're processed in order, each with its own notification.
- **The right window.** The text goes to the window that was focused when the recording stopped. If you've clicked elsewhere meanwhile, it switches back briefly to paste, then returns you to where you were. `DICTATE_FOCUS=warn` puts it on the clipboard instead, and `off` pastes wherever you are.
- **Dead mic.** A recording of pure digital silence gets a "Microphone is silent" notice that points at the audio routing, instead of a vague "no speech".
- **Paste again.** `dictate repaste`, Meta+Alt+V or the tray paste the last dictation into the focused window, for when it landed in the wrong place.
- **Faster cleanup.** The connection to the API opens while you're still talking and is reused, which saves about half a second per dictation.

**Tray icon.** A disc showing the mode's initial (C, S or R) and the state:

| Colour | State |
|---|---|
| Blue | Ready |
| Red | Recording |
| Amber, dashed ring | Working |
| Grey, struck through | Service not running |

Clicking the icon opens its menu:
- **Start/stop and cancel** the current dictation, and **paste the last dictation again**.
- **Default mode**, **preview** and **auto-stop** settings.
- **History** and the **project glossary**, which opens or creates `.claude/dictation.md`.
- **Restart service** and **hide the icon**.

Middle-click starts or stops dictation.

**Settings from the command line:**
- `dictate mode structure`, `dictate preview off` and `dictate autostop off` change the defaults. They persist, and the tray follows them.
- `dictate status` shows the current state.

**Notifications.** Every dictation uses one bubble that updates in place: Recording, Transcribing, Tidying up, then Pasted (or Copied / Cancelled / Too short).
- Each state has a fixed title, icon and lifetime.
- Problems ("Microphone is silent", "No speech heard", "Check the result", "Dictation failed") stay visible longer.
- To cut down the bubbles, set `DICTATE_NOTIFY` in `~/.config/dictate/env`:
  - `results`: skips the progress bubbles, since the tray shows them.
  - `errors`: only problems.
  - `none`: nothing.

**Project glossary.** Add `.claude/dictation.md` to projects with unusual names. To see what Haiku is given:
```bash
dictate context
```

**Health check.** This checks every piece. `--mic` also records 2 s and reports the level:
```bash
dictate doctor --mic
```

### 5. Uninstall

```bash
~/Documents/gits/claude-dictation/uninstall.sh
```
This removes the services, the command, the Python environment, the hooks (after a backup), and the installer's desktop entries and shortcuts. It keeps:
- `ydotool` and your packages.
- Shortcuts you created yourself, which it lists.
- Your settings, API key and history. `--purge` deletes these too, after asking.

`--dry-run` shows the steps without changing anything.

### 6. Test without the microphone

```bash
echo "um so like can you uh add a dash dash dry run flag to deploy dot sh" | ~/.local/share/dictate/venv/bin/python ~/Documents/gits/claude-dictation/dictate.py clean
```

## Code layout

`dictate.py` is a small launcher. The code lives in the `claude_dictation` package, one module per job:

| Module | What it does |
|---|---|
| `cli.py` | The `dictate` command and its subcommands |
| `config.py` | Paths, `DICTATE_*` settings, and the switches you change at runtime (mode, preview, auto-stop) |
| `daemon.py` | The service: one recording at a time, finished recordings queued as jobs for a single worker |
| `audio.py` | Recording with `pw-record`, pause detection for auto-stop, silence check, WAV loading |
| `transcribe.py` | faster-whisper, steered by the glossary |
| `cleanup.py` | The Haiku call with a reused client, and the structure-mode schema and layout |
| `project.py` | The current project, its context and glossary, and the Claude Code hook |
| `deliver.py` | Paste or type into the right window, with the paste key chosen per window |
| `clipboard.py` | Rich (HTML + Markdown) clipboard through `clip/wl-copy-multi` |
| `windows.py` | Focused window and switching, through `kdotool` |
| `notify.py` | The notification table and the one-bubble-per-dictation notifier |
| `history.py` | The dictation log |
| `ipc.py` | Talking to the service over its socket |
| `tray.py` | The tray icon |
| `doctor.py` | `dictate doctor` |
| `prompts/cleanup.md` | The cleanup system prompt |

At load time the modules import only the standard library, so quick commands such as the Claude Code hook stay fast. numpy, faster-whisper, anthropic and GTK load only where they're used.

Tests cover the logic that doesn't need audio or a desktop: Markdown to HTML, the structure layout, glossary terms, pause detection, paste keys and the switches. Run them with:
```bash
python3 -m unittest discover -s tests
```

## Suggestions and next steps

1. **Start with `/voice` to check the plumbing.** Once the VM hears the EasyEffects mic, Claude Code's built-in `/voice` works with no setup. Keep it for short replies ("yes, go ahead") and use this pipeline for real prompts.
2. **Grow the glossary from history.** Every dictation is logged to `~/.local/state/dictate/history.jsonl` with the raw, cleaned and final text plus timings (`DICTATE_HISTORY=0` to disable). Edits you made in the preview show where Whisper or Haiku went wrong. Add those terms to `.claude/dictation.md`.
3. **Use the history as an eval set.** Before changing the system prompt, replay 20–30 logged transcripts and compare the outputs, so a prompt change doesn't quietly start dropping details.
4. **Audio cues.** A short sound on start and stop, so you don't have to watch notifications.
5. **True hold-to-talk.** Needs reading `/dev/input` (input group, which lets any of your processes read keystrokes), a foot pedal, or a mouse button mapped to start/stop. Toggle plus auto-stop is safer.
6. **Streaming.** Show partial transcripts while you speak, or stream Haiku's output into the preview. This only matters for long dictations.
7. **Privacy.** Audio never leaves the VM. Only text and the project context (your `CLAUDE.md` and glossary) go to the Anthropic API.

## Choosing where to transcribe

- VM viewed through virt-viewer or virt-manager (SPICE): use SPICE stream routing (step 1, option A).
- Start with transcription in the VM. If `small.en` mishears too often and `large-v3-turbo` at 2.7 s feels slow, and the host has a capable GPU, run a Whisper server on the host (for example whisper.cpp with Vulkan) and point the VM daemon at it over the libvirt network. Audio capture, cleanup and delivery stay in the VM.
- `/voice` is documented for the Claude Code CLI and VS Code. Pasting works in both a terminal and the desktop app.
