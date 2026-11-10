"""Persistent settings stored as JSON in ~/.config/virt-glass/settings.json."""

import json
import os

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "virt-glass")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")

SETTINGS_VERSION = 2

DEFAULTS = {
    "settings_version": SETTINGS_VERSION,
    "libvirt_uri": "qemu:///system",
    "last_vm": None,
    "scale_mode": "fit",          # fit | stretch | integer | native
    "smooth_scaling": True,
    "auto_resize_guest": True,    # ask the guest to match the window size
    "clipboard_sync": True,
    "audio": True,
    "microphone": False,
    "show_stats_overlay": False,
    "show_sidebar": True,
    "show_panel": True,
    "wake_on_connect": True,      # nudge the mouse so a sleeping display wakes
    "screenshot_dir": os.path.expanduser("~/Pictures"),
    "upload_dir": "C:\\Users\\Public\\Downloads",
    "vm": {},                     # per-VM overrides, e.g. {"win11": {"shm": ...}}
    "geometry": None,
    "window_state": None,
}


class Settings:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.data = json.loads(json.dumps(DEFAULTS))
        try:
            with open(path) as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self.data.update(stored)
                self._migrate(stored.get("settings_version", 1))
        except (OSError, ValueError):
            pass

    def _migrate(self, version):
        if version < 2:
            # guest auto-resize became the default (matches the stock client)
            self.data["auto_resize_guest"] = True
        self.data["settings_version"] = SETTINGS_VERSION

    def __getitem__(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    def vm(self, name):
        return self.data.setdefault("vm", {}).setdefault(name, {})

    def set_vm(self, name, key, value):
        self.vm(name)[key] = value
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)
