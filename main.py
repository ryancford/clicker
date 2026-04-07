#!/usr/bin/env python3
"""Auto Clicker — GTK4 / libadwaita mouse automation tool."""

import os
# Force Cairo (software) renderer and disable explicit-sync before GTK loads.
#
# On NVidia + COSMIC, the wp_linux_drm_syncobj_v1 Wayland protocol causes
# the compositor to dup() DRM timeline fds until it hits its open-file limit
# ("import_timeline: dup failed: Too many open files").  Two guards:
#
#   GSK_RENDERER=cairo   — use CPU-side Cairo renderer; completely avoids all
#                          DRM/GPU object allocation.  Imperceptible for a
#                          simple UI like this clicker.
#   GDK_DISABLE=drm-syncobj — belt-and-suspenders: tells GTK ≥4.14 not to
#                          negotiate the explicit-sync protocol even if a
#                          GPU renderer is somehow active.
os.environ.setdefault('GSK_RENDERER', 'cairo')
os.environ.setdefault('GDK_DISABLE', 'drm-syncobj')

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gdk', '4.0')
from gi.repository import Gtk, Adw, Gdk, GLib, Gio

import json
import random
import select
import subprocess
import sys
import threading
from pathlib import Path

try:
    import evdev
    from evdev import ecodes
    EVDEV_AVAILABLE = True
    BUTTON_EVDEV = {
        'Left':   ecodes.BTN_LEFT,
        'Right':  ecodes.BTN_RIGHT,
        'Middle': ecodes.BTN_MIDDLE,
    }
except ImportError:
    EVDEV_AVAILABLE = False

CONFIG_PATH = Path.home() / '.config' / 'clicker' / 'config.json'
_COSMIC_IS_DARK = (
    Path.home() / '.config' / 'cosmic' /
    'com.system76.CosmicTheme.Mode' / 'v1' / 'is_dark'
)

BUTTON_LABELS = ['Left', 'Right', 'Middle']
BUTTON_EVDEV  = {}  # populated after evdev import check below

# Pure-modifier keysyms — don't record these as hotkeys on their own
_MODIFIER_KEYSYMS = frozenset([
    Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Shift_L,   Gdk.KEY_Shift_R,
    Gdk.KEY_Alt_L,     Gdk.KEY_Alt_R,
    Gdk.KEY_Super_L,   Gdk.KEY_Super_R,
    Gdk.KEY_ISO_Level3_Shift,
    Gdk.KEY_Caps_Lock, Gdk.KEY_Num_Lock, Gdk.KEY_Scroll_Lock,
])

# GDK key name → evdev KEY_* name
_GDK_NAME_TO_EVDEV: dict[str, str] = {
    **{chr(c): f'KEY_{chr(c).upper()}' for c in range(ord('a'), ord('z') + 1)},
    **{f'F{i}': f'KEY_F{i}' for i in range(1, 13)},
    **{str(i): f'KEY_{i}' for i in range(10)},
    'Return':      'KEY_ENTER',
    'KP_Enter':    'KEY_KPENTER',
    'Tab':         'KEY_TAB',
    'space':       'KEY_SPACE',
    'BackSpace':   'KEY_BACKSPACE',
    'Escape':      'KEY_ESC',
    'Delete':      'KEY_DELETE',
    'Insert':      'KEY_INSERT',
    'Home':        'KEY_HOME',
    'End':         'KEY_END',
    'Page_Up':     'KEY_PAGEUP',
    'Page_Down':   'KEY_PAGEDOWN',
    'Left':        'KEY_LEFT',
    'Right':       'KEY_RIGHT',
    'Up':          'KEY_UP',
    'Down':        'KEY_DOWN',
    'Print':       'KEY_SYSRQ',
    'Pause':       'KEY_PAUSE',
    'minus':       'KEY_MINUS',
    'equal':       'KEY_EQUAL',
    'bracketleft': 'KEY_LEFTBRACE',
    'bracketright':'KEY_RIGHTBRACE',
    'semicolon':   'KEY_SEMICOLON',
    'apostrophe':  'KEY_APOSTROPHE',
    'grave':       'KEY_GRAVE',
    'backslash':   'KEY_BACKSLASH',
    'comma':       'KEY_COMMA',
    'period':      'KEY_DOT',
    'slash':       'KEY_SLASH',
}

# evdev key code → canonical modifier token
_EVDEV_MOD_CODES: dict[int, str] = {}
if EVDEV_AVAILABLE:
    _EVDEV_MOD_CODES = {
        ecodes.KEY_LEFTCTRL:  'ctrl',
        ecodes.KEY_RIGHTCTRL: 'ctrl',
        ecodes.KEY_LEFTALT:   'alt',
        ecodes.KEY_RIGHTALT:  'alt',
        ecodes.KEY_LEFTSHIFT: 'shift',
        ecodes.KEY_RIGHTSHIFT:'shift',
        ecodes.KEY_LEFTMETA:  'super',
        ecodes.KEY_RIGHTMETA: 'super',
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def ydotoold_running() -> bool:
    try:
        return subprocess.run(['pgrep', 'ydotoold'], capture_output=True).returncode == 0
    except Exception:
        return False


def _ensure_ydotoold() -> bool:
    """Start ydotoold in the background if not already running. Returns True if running."""
    if ydotoold_running():
        return True
    try:
        subprocess.Popen(['ydotoold'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time; time.sleep(0.2)  # brief wait for socket to be ready
        return True
    except FileNotFoundError:
        print('ydotoold not found — install ydotool', file=sys.stderr)
        return False
    except Exception as exc:
        print(f'Failed to start ydotoold: {exc}', file=sys.stderr)
        return False


def _start_ydotoold_watchdog(banner) -> None:
    """Daemon thread: keep ydotoold alive and sync the warning banner."""
    import time

    def _watchdog():
        last_state = ydotoold_running()
        while True:
            time.sleep(5)
            running = ydotoold_running()
            if not running:
                print('ydotoold died — restarting…', file=sys.stderr)
                running = _ensure_ydotoold()
            if running != last_state:
                last_state = running
                GLib.idle_add(banner.set_revealed, not running)

    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()


def _gdk_to_hotkey(keyval: int, state: Gdk.ModifierType) -> tuple[str, str]:
    """Return (hotkey_string, human_display_string) from a GTK key event.

    hotkey_string tokens are separated by '+':
      modifier tokens : 'ctrl', 'alt', 'shift', 'super'
      key tokens      : evdev KEY_* names, e.g. 'KEY_F6', 'KEY_A'
    """
    parts: list[str] = []
    display: list[str] = []

    if state & Gdk.ModifierType.CONTROL_MASK:
        parts.append('ctrl');  display.append('Ctrl')
    if state & Gdk.ModifierType.ALT_MASK:
        parts.append('alt');   display.append('Alt')
    if state & Gdk.ModifierType.SUPER_MASK:
        parts.append('super'); display.append('Super')
    if state & Gdk.ModifierType.SHIFT_MASK:
        parts.append('shift'); display.append('Shift')

    name = Gdk.keyval_name(keyval) or ''
    evdev_name = _GDK_NAME_TO_EVDEV.get(name, f'KEY_{name.upper()}')
    parts.append(evdev_name)
    display.append(name if len(name) > 1 else name.upper())

    return '+'.join(parts), ' + '.join(display)


_KEYBOARD_REQUIRED_KEYS = frozenset({
    ecodes.KEY_A, ecodes.KEY_Z, ecodes.KEY_SPACE,
    ecodes.KEY_ENTER, ecodes.KEY_ESC, ecodes.KEY_LEFTSHIFT,
})
# Devices that report relative motion are mice/touchpads, not keyboards
_MOUSE_CAPS = frozenset({ecodes.REL_X, ecodes.REL_Y})

def _evdev_keyboards() -> list:
    """Return evdev InputDevice objects for physical keyboards only.

    Requires a core set of keyboard keys and excludes any device that
    also reports relative-motion events (mice, touchpads, gaming mice
    with macro-key keyboard interfaces, etc.).
    """
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities()
            keys = set(caps.get(ecodes.EV_KEY, []))
            rel  = set(caps.get(ecodes.EV_REL, []))
            if _KEYBOARD_REQUIRED_KEYS.issubset(keys) and not (_MOUSE_CAPS & rel):
                devices.append(dev)
            else:
                dev.close()
        except Exception:
            pass
    return devices


# ── Application ────────────────────────────────────────────────────────────

class ClickerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.clicker')
        self.connect('activate', self._on_activate)

    def _on_activate(self, app):
        # Apply saved theme before the window is created to avoid a flash
        config = {}
        if CONFIG_PATH.exists():
            try:
                config = json.loads(CONFIG_PATH.read_text())
            except Exception:
                pass
        theme = config.get('theme', 'system')
        sm = Adw.StyleManager.get_default()
        gs = Gtk.Settings.get_default()
        if theme == 'light':
            sm.set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
            gs.set_property('gtk-theme-name', 'adw-gtk3')
        elif theme == 'dark':
            sm.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            gs.set_property('gtk-theme-name', 'adw-gtk3-dark')
        else:  # system
            if _COSMIC_IS_DARK.exists():
                try:
                    dark = _COSMIC_IS_DARK.read_text().strip().lower() == 'true'
                    sm.set_color_scheme(
                        Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
                    gs.set_property('gtk-theme-name',
                                    'adw-gtk3-dark' if dark else 'adw-gtk3')
                except Exception:
                    sm.set_color_scheme(Adw.ColorScheme.DEFAULT)
            else:
                sm.set_color_scheme(Adw.ColorScheme.DEFAULT)

        MainWindow(application=app).present()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title('Auto Clicker')
        self.set_default_size(480, 720)
        self.set_icon_name('io.github.clicker')

        self.clicking      = False
        self.stop_event    = threading.Event()
        self.click_thread: threading.Thread | None = None
        self._hotkey_stop: threading.Event | None = None
        self._portal_conn = None
        self._portal_sub_id = None
        self._theme_monitor = None
        self._system_mode = False
        self._uinput: evdev.UInput | None = None

        self.config = self._load_config()
        self._build_ui()
        self._apply_theme(self.config.get('theme', 'system'))
        self._theme_system_btn.connect('toggled', self._on_theme_radio_toggled)
        self._theme_light_btn.connect('toggled', self._on_theme_radio_toggled)
        self._theme_dark_btn.connect('toggled', self._on_theme_radio_toggled)
        self._setup_hotkey()
        self._init_uinput()
        self.connect('close-request', self._on_close_request)

    # ── Config ─────────────────────────────────────────────────────────────

    def _load_config(self) -> dict:
        if CONFIG_PATH.exists():
            try:
                return json.loads(CONFIG_PATH.read_text())
            except Exception:
                pass
        return {}

    def _save_config(self):
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.config, indent=2))

    # ── UI ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        view_stack = Adw.ViewStack()
        switcher   = Adw.ViewSwitcher()
        switcher.set_stack(view_stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        view_stack.add_titled_with_icon(
            self._build_clicker_page(), 'clicker', 'Clicker', 'input-mouse-symbolic')
        view_stack.add_titled_with_icon(
            self._build_settings_page(), 'settings', 'Settings', 'preferences-system-symbolic')

        toolbar_view.set_content(view_stack)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(toolbar_view)
        toolbar_view.set_vexpand(True)
        self.set_content(outer)

    # ── Clicker page ────────────────────────────────────────────────────────

    def _build_clicker_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        box.append(self._build_target_section())
        box.append(self._build_button_section())
        box.append(self._build_timing_section())
        box.append(self._build_count_section())
        box.append(self._build_hotkey_section())
        box.append(self._build_start_section())

        scroll.set_child(box)
        return scroll

    def _build_target_section(self) -> Gtk.Widget:
        cfg = self.config
        use_fixed = cfg.get('target') == 'fixed'

        mode_group = Adw.PreferencesGroup()
        mode_group.set_title('Click Target')

        self._current_radio = Gtk.CheckButton()
        self._current_radio.set_active(not use_fixed)
        current_row = Adw.ActionRow()
        current_row.set_title('Current cursor position')
        current_row.add_prefix(self._current_radio)
        current_row.set_activatable_widget(self._current_radio)

        self._fixed_radio = Gtk.CheckButton()
        self._fixed_radio.set_group(self._current_radio)
        self._fixed_radio.set_active(use_fixed)
        fixed_row = Adw.ActionRow()
        fixed_row.set_title('Fixed position')
        fixed_row.add_prefix(self._fixed_radio)
        fixed_row.set_activatable_widget(self._fixed_radio)

        mode_group.add(current_row)
        mode_group.add(fixed_row)

        # Coordinate row (no title — just the widgets)
        self._coord_group = Adw.PreferencesGroup()
        coord_row = Adw.ActionRow()

        coord_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        coord_box.set_valign(Gtk.Align.CENTER)

        self._x_spin = Gtk.SpinButton()
        self._x_spin.set_adjustment(
            Gtk.Adjustment(value=cfg.get('x', 0), lower=0, upper=9999, step_increment=1))
        self._x_spin.set_width_chars(6)
        self._x_spin.set_tooltip_text('X coordinate')

        self._y_spin = Gtk.SpinButton()
        self._y_spin.set_adjustment(
            Gtk.Adjustment(value=cfg.get('y', 0), lower=0, upper=9999, step_increment=1))
        self._y_spin.set_width_chars(6)
        self._y_spin.set_tooltip_text('Y coordinate')

        self._get_btn = Gtk.Button(label='Get Position')
        self._get_btn.add_css_class('suggested-action')
        self._get_btn.set_tooltip_text('Click anywhere on screen to capture coordinates')
        self._get_btn.connect('clicked', self._on_get_position)

        coord_box.append(Gtk.Label(label='X:'))
        coord_box.append(self._x_spin)
        coord_box.append(Gtk.Label(label='Y:'))
        coord_box.append(self._y_spin)
        coord_box.append(self._get_btn)
        coord_row.add_suffix(coord_box)
        self._coord_group.add(coord_row)
        self._coord_group.set_sensitive(use_fixed)

        def on_mode_changed(_btn):
            self._coord_group.set_sensitive(self._fixed_radio.get_active())

        self._current_radio.connect('toggled', on_mode_changed)
        self._fixed_radio.connect('toggled', on_mode_changed)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        wrapper.append(mode_group)
        wrapper.append(self._coord_group)
        return wrapper

    def _build_button_section(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        group.set_title('Mouse Button')
        self._button_combo = Adw.ComboRow()
        self._button_combo.set_title('Button')
        self._button_combo.set_model(Gtk.StringList.new(BUTTON_LABELS))
        self._button_combo.set_selected(self.config.get('button', 0))
        group.add(self._button_combo)
        return group

    def _build_timing_section(self) -> Gtk.Widget:
        cfg = self.config
        group = Adw.PreferencesGroup()
        group.set_title('Timing')

        delay_row = Adw.ActionRow()
        delay_row.set_title('Delay (ms)')
        delay_row.set_subtitle('Milliseconds between clicks')
        self._delay_spin = Gtk.SpinButton()
        self._delay_spin.set_adjustment(
            Gtk.Adjustment(value=cfg.get('delay', 1000), lower=1, upper=60000, step_increment=100))
        self._delay_spin.set_width_chars(7)
        self._delay_spin.set_valign(Gtk.Align.CENTER)
        delay_row.add_suffix(self._delay_spin)

        offset_row = Adw.ActionRow()
        offset_row.set_title('Random offset (ms)')
        offset_row.set_subtitle('±ms added randomly to each delay  •  0 = disabled')
        self._offset_spin = Gtk.SpinButton()
        self._offset_spin.set_adjustment(
            Gtk.Adjustment(value=cfg.get('offset', 0), lower=0, upper=10000, step_increment=100))
        self._offset_spin.set_width_chars(7)
        self._offset_spin.set_valign(Gtk.Align.CENTER)
        offset_row.add_suffix(self._offset_spin)

        group.add(delay_row)
        group.add(offset_row)
        return group

    def _build_count_section(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        group.set_title('Click Count')

        count_row = Adw.ActionRow()
        count_row.set_title('Repeat count')
        count_row.set_subtitle('0 = click indefinitely')
        self._count_spin = Gtk.SpinButton()
        self._count_spin.set_adjustment(
            Gtk.Adjustment(value=self.config.get('count', 0),
                           lower=0, upper=999999, step_increment=1))
        self._count_spin.set_width_chars(8)
        self._count_spin.set_valign(Gtk.Align.CENTER)
        count_row.add_suffix(self._count_spin)

        group.add(count_row)
        return group

    def _build_hotkey_section(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup()
        group.set_title('Hotkey')

        row = Adw.ActionRow()
        row.set_title('Start / Stop hotkey')
        if not EVDEV_AVAILABLE:
            row.set_subtitle('Install python3-evdev to enable hotkeys')

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_valign(Gtk.Align.CENTER)

        self._hotkey_label = Gtk.Label(
            label=self.config.get('hotkey_display', 'Not set'))
        self._hotkey_label.add_css_class('monospace')
        self._hotkey_label.add_css_class('dim-label')

        self._set_hotkey_btn = Gtk.Button(label='Set')
        self._set_hotkey_btn.connect('clicked', self._on_record_hotkey)

        self._clear_hotkey_btn = Gtk.Button(label='Clear')
        self._clear_hotkey_btn.add_css_class('destructive-action')
        self._clear_hotkey_btn.connect('clicked', self._on_clear_hotkey)

        btn_box.append(self._hotkey_label)
        btn_box.append(self._set_hotkey_btn)
        btn_box.append(self._clear_hotkey_btn)
        row.add_suffix(btn_box)
        group.add(row)
        return group

    def _build_start_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self._start_btn = Gtk.Button(label='Start')
        self._start_btn.add_css_class('suggested-action')
        self._start_btn.add_css_class('pill')
        self._start_btn.set_hexpand(True)
        self._start_btn.connect('clicked', self._on_start_stop)

        self._status_label = Gtk.Label(label='Ready')
        self._status_label.add_css_class('dim-label')

        box.append(self._start_btn)
        box.append(self._status_label)
        return box

    # ── Settings page ───────────────────────────────────────────────────────

    def _build_settings_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        group = Adw.PreferencesGroup()
        group.set_title('Appearance')
        group.set_description('Choose how the application looks')

        saved = self.config.get('theme', 'system')

        self._theme_system_btn = Gtk.CheckButton()
        self._theme_system_btn.set_active(saved == 'system')
        system_row = Adw.ActionRow()
        system_row.set_title('System')
        system_row.set_subtitle('Follow the system light/dark preference')
        system_row.add_prefix(self._theme_system_btn)
        system_row.set_activatable_widget(self._theme_system_btn)

        self._theme_light_btn = Gtk.CheckButton()
        self._theme_light_btn.set_group(self._theme_system_btn)
        self._theme_light_btn.set_active(saved == 'light')
        light_row = Adw.ActionRow()
        light_row.set_title('Light')
        light_row.add_prefix(self._theme_light_btn)
        light_row.set_activatable_widget(self._theme_light_btn)

        self._theme_dark_btn = Gtk.CheckButton()
        self._theme_dark_btn.set_group(self._theme_system_btn)
        self._theme_dark_btn.set_active(saved == 'dark')
        dark_row = Adw.ActionRow()
        dark_row.set_title('Dark')
        dark_row.add_prefix(self._theme_dark_btn)
        dark_row.set_activatable_widget(self._theme_dark_btn)

        group.add(system_row)
        group.add(light_row)
        group.add(dark_row)
        box.append(group)
        return box

    # ── Theme ───────────────────────────────────────────────────────────────

    def _apply_theme(self, theme: str):
        self._disconnect_system_theme()
        if theme == 'system':
            self._system_mode = True
            self._apply_system_theme()
        else:
            self._system_mode = False
            dark = (theme == 'dark')
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
            self._set_gtk_theme_name(dark)

    def _set_gtk_theme_name(self, dark: bool):
        """Switch gtk-theme-name between adw-gtk3 and adw-gtk3-dark.

        COSMIC sets gtk-theme-name=adw-gtk3-dark (a fixed dark CSS theme) on
        all GTK4 apps. Libadwaita's FORCE_LIGHT/FORCE_DARK only affects
        libadwaita's own color variables; if the GTK theme CSS hard-codes dark
        colors on top, the visual result is always dark. Switching the theme
        name to the correct light/dark variant is required.
        """
        theme = 'adw-gtk3-dark' if dark else 'adw-gtk3'
        Gtk.Settings.get_default().set_property('gtk-theme-name', theme)

    def _apply_system_theme(self):
        """Apply the system theme and watch for live changes.

        On COSMIC the authoritative source is
        ~/.config/cosmic/com.system76.CosmicTheme.Mode/v1/is_dark.
        We read it immediately and use a GIO file monitor for live updates.
        On non-COSMIC systems (GNOME etc.) we fall back to DEFAULT + the XDG
        portal SettingChanged signal.
        """
        if _COSMIC_IS_DARK.exists():
            self._apply_cosmic_system_theme()
        else:
            self._apply_portal_system_theme()

    def _apply_cosmic_system_theme(self):
        dark = self._read_cosmic_is_dark()
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
        self._set_gtk_theme_name(dark)

        f = Gio.File.new_for_path(str(_COSMIC_IS_DARK))
        mon = f.monitor_file(Gio.FileMonitorFlags.NONE, None)
        mon.connect('changed', self._on_cosmic_theme_changed)
        self._theme_monitor = mon

    def _read_cosmic_is_dark(self) -> bool:
        try:
            return _COSMIC_IS_DARK.read_text().strip().lower() == 'true'
        except Exception:
            return False

    def _on_cosmic_theme_changed(self, monitor, file, other, event_type):
        if not self._system_mode:
            return
        if event_type not in (Gio.FileMonitorEvent.CHANGED,
                               Gio.FileMonitorEvent.CREATED):
            return
        dark = self._read_cosmic_is_dark()
        Adw.StyleManager.get_default().set_color_scheme(
            Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT)
        self._set_gtk_theme_name(dark)

    def _apply_portal_system_theme(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.DEFAULT)
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._portal_conn = conn
            self._portal_sub_id = conn.signal_subscribe(
                'org.freedesktop.portal.Desktop',
                'org.freedesktop.portal.Settings',
                'SettingChanged',
                '/org/freedesktop/portal/desktop',
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_portal_setting_changed,
            )
        except Exception as exc:
            print(f'Portal unavailable ({exc})', file=sys.stderr)

    def _on_portal_setting_changed(self, conn, sender, path, iface, signal, params):
        if not self._system_mode:
            return
        if params.get_child_value(0).get_string() != 'org.freedesktop.appearance':
            return
        if params.get_child_value(1).get_string() != 'color-scheme':
            return
        v = params.get_child_value(2)
        while v.get_type_string() == 'v':
            v = v.get_variant()
        try:
            dark = (v.get_uint32() == 1)
        except Exception:
            return
        scheme = Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT
        GLib.idle_add(Adw.StyleManager.get_default().set_color_scheme, scheme)

    def _disconnect_system_theme(self):
        self._system_mode = False
        if self._theme_monitor:
            self._theme_monitor.cancel()
            self._theme_monitor = None
        if self._portal_conn and self._portal_sub_id is not None:
            self._portal_conn.signal_unsubscribe(self._portal_sub_id)
        self._portal_sub_id = None
        self._portal_conn = None

    def _on_theme_radio_toggled(self, btn):
        if not btn.get_active():
            return
        if btn is self._theme_system_btn:
            theme = 'system'
        elif btn is self._theme_light_btn:
            theme = 'light'
        else:
            theme = 'dark'
        self._apply_theme(theme)
        self.config['theme'] = theme
        self._save_config()

    # ── Get Position ────────────────────────────────────────────────────────

    def _on_get_position(self, _btn):
        self._get_btn.set_label('Capturing…')
        self._get_btn.set_sensitive(False)

        overlay = Gtk.Window(application=self.get_application())
        overlay.set_decorated(False)
        overlay.fullscreen()
        overlay.set_cursor_from_name('crosshair')

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        heading = Gtk.Label(label='Click anywhere to capture position')
        heading.add_css_class('title-1')
        sub = Gtk.Label(label='Escape to cancel')
        sub.add_css_class('dim-label')
        box.append(heading)
        box.append(sub)
        overlay.set_child(box)

        captured = {'done': False}

        click_ctrl = Gtk.GestureClick()
        def on_pressed(gesture, _n, x, y):
            captured['done'] = True
            scale = overlay.get_scale_factor()
            overlay.close()
            GLib.idle_add(self._position_captured, int(x * scale), int(y * scale))
        click_ctrl.connect('pressed', on_pressed)
        overlay.add_controller(click_ctrl)

        key_ctrl = Gtk.EventControllerKey()
        def on_key(_ctrl, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape:
                overlay.close()
                return True
        key_ctrl.connect('key-pressed', on_key)
        overlay.add_controller(key_ctrl)

        def on_close_request(_win):
            if not captured['done']:
                self._get_btn.set_label('Get Position')
                self._get_btn.set_sensitive(True)
            return False

        overlay.connect('close-request', on_close_request)
        overlay.present()

    def _position_captured(self, x: int, y: int) -> bool:
        self._x_spin.set_value(x)
        self._y_spin.set_value(y)
        self._get_btn.set_label('Get Position')
        self._get_btn.set_sensitive(True)
        return False

    # ── Hotkey Recording ────────────────────────────────────────────────────

    def _on_record_hotkey(self, _btn):
        """Show a dialog and capture the next key combo via GTK event controller."""
        recorded: dict = {}

        dialog = Adw.AlertDialog()
        dialog.set_heading('Set Hotkey')
        dialog.set_body('Press any key or combination…\n\nEsc to cancel.')
        dialog.add_response('cancel', 'Cancel')

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def on_key_pressed(_ctrl, keyval, _keycode, state):
            if keyval in _MODIFIER_KEYSYMS:
                return False
            if keyval == Gdk.KEY_Escape:
                dialog.close()
                return True

            hotkey, display = _gdk_to_hotkey(keyval, state)
            recorded['hotkey']  = hotkey
            recorded['display'] = display
            dialog.close()
            return True

        key_ctrl.connect('key-pressed', on_key_pressed)
        dialog.add_controller(key_ctrl)

        def on_closed(_dlg):
            hotkey = recorded.get('hotkey')
            if hotkey:
                self.config['hotkey']         = hotkey
                self.config['hotkey_display'] = recorded['display']
                self._save_config()
                self._hotkey_label.set_label(recorded['display'])
                self._setup_hotkey()

        dialog.connect('closed', on_closed)
        dialog.present(self)

    def _on_clear_hotkey(self, _btn):
        self.config.pop('hotkey', None)
        self.config.pop('hotkey_display', None)
        self._save_config()
        self._hotkey_label.set_label('Not set')
        self._stop_hotkey_listener()

    # ── Hotkey Listener (evdev) ─────────────────────────────────────────────

    def _stop_hotkey_listener(self):
        if self._hotkey_stop:
            self._hotkey_stop.set()
            self._hotkey_stop = None

    def _setup_hotkey(self):
        self._stop_hotkey_listener()

        hotkey_str = self.config.get('hotkey')
        if not hotkey_str or not EVDEV_AVAILABLE:
            return

        target: set[str] = set(hotkey_str.split('+'))
        stop = threading.Event()
        self._hotkey_stop = stop

        def listener():
            import time

            while not stop.is_set():
                devices = _evdev_keyboards()
                if not devices:
                    print('evdev: no keyboard devices found, retrying in 5 s…', file=sys.stderr)
                    GLib.idle_add(self._status_label.set_label,
                                  'Hotkey: no input devices (check input group)')
                    time.sleep(5)
                    continue

                print(f'evdev hotkey listener: monitoring {len(devices)} device(s) '
                      f'for {hotkey_str}', file=sys.stderr)

                # Reset key state on each (re-)enumeration so stale pressed/fired
                # state from before a device disconnect never blocks the hotkey.
                pressed: set[str] = set()
                fired = False

                # poll() has no FD_SETSIZE (1024) limit unlike select().
                poller = select.poll()
                fd_map: dict[int, evdev.InputDevice] = {}
                for dev in devices:
                    poller.register(dev, select.POLLIN)
                    fd_map[dev.fd] = dev

                while not stop.is_set() and fd_map:
                    try:
                        ready = poller.poll(500)  # ms
                    except OSError:
                        break

                    for fd, evt_mask in ready:
                        dev = fd_map.get(fd)
                        if dev is None:
                            continue
                        if evt_mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                            poller.unregister(fd)
                            del fd_map[fd]
                            try:
                                dev.close()
                            except Exception:
                                pass
                            continue
                        try:
                            for event in dev.read():
                                if event.type != ecodes.EV_KEY:
                                    continue
                                # Translate code → canonical token
                                token = _EVDEV_MOD_CODES.get(event.code)
                                if token is None:
                                    names = ecodes.KEY.get(event.code, '')
                                    token = (names[0] if isinstance(names, list)
                                             else names) or None
                                if not token:
                                    continue

                                if event.value == 1:       # key down
                                    pressed.add(token)
                                    print(f'[hotkey] ↓ {token}  pressed={pressed}  target={target}',
                                          file=sys.stderr)
                                    if not fired and target <= pressed:
                                        fired = True
                                        GLib.idle_add(self._on_start_stop, None)
                                elif event.value == 0:     # key up
                                    pressed.discard(token)
                                    if fired and not (target <= pressed):
                                        fired = False
                        except OSError:
                            poller.unregister(fd)
                            del fd_map[fd]
                            try:
                                dev.close()
                            except Exception:
                                pass

                # Close any remaining devices before re-enumerating.
                for dev in fd_map.values():
                    try:
                        dev.close()
                    except Exception:
                        pass

                if not stop.is_set():
                    # Brief pause before re-enumerating to avoid a tight loop
                    # if devices keep failing immediately.
                    print('evdev: device(s) lost, re-enumerating in 2 s…', file=sys.stderr)
                    time.sleep(2)

        t = threading.Thread(target=listener, daemon=True)
        t.start()

    # ── Click loop ──────────────────────────────────────────────────────────

    def _on_start_stop(self, _btn):
        if self.clicking:
            self._stop_clicking()
        else:
            self._start_clicking()

    def _start_clicking(self):
        self.clicking = True
        self.stop_event.clear()
        self._start_btn.set_label('Stop')
        self._start_btn.remove_css_class('suggested-action')
        self._start_btn.add_css_class('destructive-action')

        button_code = BUTTON_EVDEV.get(BUTTON_LABELS[self._button_combo.get_selected()],
                                       ecodes.BTN_LEFT)
        delay_ms    = self._delay_spin.get_value()
        offset_ms   = self._offset_spin.get_value()
        count       = int(self._count_spin.get_value())
        use_fixed   = self._fixed_radio.get_active()
        x           = int(self._x_spin.get_value())
        y           = int(self._y_spin.get_value())

        self.click_thread = threading.Thread(
            target=self._click_loop,
            args=(button_code, delay_ms, offset_ms, count, use_fixed, x, y),
            daemon=True,
        )
        self.click_thread.start()

    def _stop_clicking(self):
        self.clicking = False
        self.stop_event.set()
        self._start_btn.set_label('Start')
        self._start_btn.remove_css_class('destructive-action')
        self._start_btn.add_css_class('suggested-action')
        GLib.idle_add(self._status_label.set_label, 'Ready')

    def _click_loop(self, button_code, delay_ms, offset_ms, count, use_fixed, x, y):
        ui = self._uinput
        if ui is None:
            GLib.idle_add(self._status_label.set_label, 'UInput unavailable')
            GLib.idle_add(self._stop_clicking)
            return

        clicks_done = 0
        while not self.stop_event.is_set():
            if count > 0 and clicks_done >= count:
                GLib.idle_add(self._stop_clicking)
                break

            try:
                if use_fixed:
                    # Slam cursor to top-left then move to target
                    ui.write(ecodes.EV_REL, ecodes.REL_X, -99999)
                    ui.write(ecodes.EV_REL, ecodes.REL_Y, -99999)
                    ui.syn()
                    ui.write(ecodes.EV_REL, ecodes.REL_X, x)
                    ui.write(ecodes.EV_REL, ecodes.REL_Y, y)
                    ui.syn()

                ui.write(ecodes.EV_KEY, button_code, 1)  # press
                ui.syn()
                ui.write(ecodes.EV_KEY, button_code, 0)  # release
                ui.syn()

            except OSError as exc:
                GLib.idle_add(self._status_label.set_label, f'Input error: {exc}')
                GLib.idle_add(self._stop_clicking)
                break

            clicks_done += 1
            label = (f'Clicking… ({clicks_done})'
                     if count == 0 else
                     f'Clicking… ({clicks_done} / {count})')
            GLib.idle_add(self._status_label.set_label, label)

            sleep_ms = delay_ms
            if offset_ms > 0:
                sleep_ms += random.uniform(-offset_ms, offset_ms)
            self.stop_event.wait(max(1.0, sleep_ms) / 1000.0)

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _init_uinput(self):
        """Create the persistent UInput device used for all click injection.

        Created once at startup rather than per click session so that the
        Wayland compositor only sees one udev 'device added' event total.
        Repeated create/destroy cycles caused COSMIC to accumulate open fds
        for each virtual device, eventually exhausting its own fd table.
        """
        if not EVDEV_AVAILABLE:
            return
        try:
            self._uinput = evdev.UInput(
                {
                    ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
                    ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y],
                },
                name='auto-clicker',
            )
        except Exception as exc:
            print(f'UInput init failed: {exc}', file=sys.stderr)

    def _on_close_request(self, _win) -> bool:
        self._stop_hotkey_listener()
        self._disconnect_system_theme()
        self._save_all_settings()
        if self._uinput:
            try:
                self._uinput.close()
            except Exception:
                pass
        return False  # allow the window to close

    def _save_all_settings(self):
        self.config['button'] = self._button_combo.get_selected()
        self.config['delay']  = self._delay_spin.get_value()
        self.config['offset'] = self._offset_spin.get_value()
        self.config['count']  = int(self._count_spin.get_value())
        self.config['target'] = 'fixed' if self._fixed_radio.get_active() else 'current'
        self.config['x']      = int(self._x_spin.get_value())
        self.config['y']      = int(self._y_spin.get_value())
        self._save_config()

    # ── Daemon help dialog ──────────────────────────────────────────────────

# ── Entry point ────────────────────────────────────────────────────────────

def _install_assets():
    """Install icon and .desktop file to ~/.local/share on first run."""
    here = Path(__file__).parent

    targets = [
        (here / 'icon.svg',
         Path.home() / '.local/share/icons/hicolor/scalable/apps/io.github.clicker.svg'),
    ]

    desktop_src = here / 'io.github.clicker.desktop'
    desktop_dst = Path.home() / '.local/share/applications/io.github.clicker.desktop'

    installed = False
    for src, dst in targets:
        if src.exists() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
                installed = True
            except Exception as exc:
                print(f'Could not install {dst.name}: {exc}', file=sys.stderr)

    # Install .desktop with the correct absolute path to this script
    if desktop_src.exists() and not desktop_dst.exists():
        try:
            desktop_dst.parent.mkdir(parents=True, exist_ok=True)
            content = desktop_src.read_text().replace(
                '__MAIN_PY__', str(here / 'main.py'))
            desktop_dst.write_text(content)
            installed = True
        except Exception as exc:
            print(f'Could not install desktop file: {exc}', file=sys.stderr)

    if installed:
        try:
            subprocess.run(['update-desktop-database',
                            str(Path.home() / '.local/share/applications')],
                           capture_output=True)
            subprocess.run(['gtk-update-icon-cache', '-f', '-t',
                            str(Path.home() / '.local/share/icons/hicolor')],
                           capture_output=True)
        except Exception:
            pass


def main():
    _install_assets()
    app = ClickerApp()
    try:
        return app.run(sys.argv)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
