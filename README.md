# Auto Clicker

A GTK4 / libadwaita mouse automation tool for Linux with Wayland support.

## Features

- **Click target** — click at the current cursor position or a fixed coordinate
- **Mouse button** — left, right, or middle click
- **Timing** — configurable delay between clicks with optional random offset for human-like patterns
- **Click count** — repeat a set number of times or click indefinitely
- **Global hotkey** — start/stop clicking from anywhere on screen (requires `input` group membership on Wayland)
- **Theme** — system, light, or dark
- **ydotoold** — automatically starts the ydotoold daemon on launch if it isn't already running

## Requirements

### System packages

```bash
sudo apt install ydotool python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1
```

### Python packages

```bash
sudo apt install python3-pynput
# or
pip install pynput
```

### Wayland / hotkey permissions

Global hotkeys are read directly from `/dev/input/event*` via the evdev backend.
Your user must be in the `input` group:

```bash
sudo usermod -aG input $USER
```

Log out and back in for the change to take effect.

## Usage

```bash
python3 main.py
```

Settings are saved automatically to `~/.config/clicker/config.json`.

## ydotool daemon

Clicks are sent via `ydotool`, which requires the `ydotoold` daemon to be running.
The app starts it automatically on launch. To keep it running across reboots, create a systemd user service:

```bash
systemctl --user enable --now ydotoold
```

Or add `ydotoold &` to your session startup script.

If ydotoold is not running, a banner will appear at the top of the window with instructions.
