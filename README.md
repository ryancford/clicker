# Auto Clicker

A GTK4 / libadwaita mouse automation tool for Linux with Wayland support.

## Features

- **Click target** — click at the current cursor position or a fixed coordinate
- **Mouse button** — left, right, or middle click
- **Timing** — configurable delay between clicks with optional random offset for human-like patterns
- **Click count** — repeat a set number of times or click indefinitely
- **Global hotkey** — start/stop clicking from anywhere on screen
- **Theme** — system (follows COSMIC/GNOME dark/light setting), light, or dark

## Requirements

### System packages

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-evdev
```

### Permissions

Clicks are injected directly via `/dev/uinput` and the global hotkey listener reads from `/dev/input/event*`. Your user must be in the `input` group:

```bash
sudo usermod -aG input $USER
```

Log out and back in for the change to take effect.

## Usage

```bash
python3 main.py
```

Settings are saved automatically to `~/.config/clicker/config.json`.

## How it works

- **Clicking** — injects mouse button events directly via a persistent `evdev.UInput` virtual device. No daemon or external process required.
- **Global hotkey** — reads keyboard events directly from `/dev/input/event*` using `python3-evdev`, bypassing the compositor so hotkeys work on Wayland regardless of which window is focused.
- **System theme** — queries the XDG Desktop Portal (`org.freedesktop.portal.Settings`) for the current dark/light preference and watches `~/.config/cosmic/com.system76.CosmicTheme.Mode/` for live updates on COSMIC desktop.
