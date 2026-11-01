"""Prompt Vault (desktop) — pywebview host.

Runs the existing Prompt Vault UI in an OS webview and gives it a real
filesystem underneath instead of browser storage.

Design notes worth keeping:
  * The encryption key lives in the Vault object, in Python, and is never
    passed to JavaScript. JS holds the decrypted db in memory only.
  * Every Api method returns {"ok": bool, ...}. The UI's save path depends on
    an honest boolean — a save that failed must never report success. That
    contract is the whole reason v3.16 existed; it survives the port.
  * Nothing here opens a socket. pywebview renders a local file; there is no
    HTTP server, no localhost binding, no update check.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import webview

from vault import (
    Settings, Vault, VaultError, BadPin, EMPTY_DB, GENERATIONS,
)

APP_VERSION = "6.1"
APP_BUILD = "2026-11-02"

# Beside the app whether we're a script or a PyInstaller one-file bundle.
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
HOME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent

UI_FILE = BASE_DIR / "ui" / "index.html"
SETTINGS_FILE = HOME_DIR / "promptvault-settings.json"
DEFAULT_VAULT = HOME_DIR / "promptvault.vault"

SEED_FILE = BASE_DIR / "ui" / "seed.json"


def _seed_db():
    try:
        return json.loads(SEED_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return dict(EMPTY_DB)


def count_library(db):
    """Import summary. v6.0: a card (an entry in db.prompts) may carry up to 9
    alternates in `variants`, so `prompts` counts every prompt including
    variants, `cards` counts entries, and `images` counts every variant's image.
    `protected` is card-level, like the flag itself."""
    cards = [p for p in (db.get("prompts") or []) if isinstance(p, dict)]
    def variants(p):
        v = p.get("variants")
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []
    return {
        "cards": len(cards),
        "prompts": sum(1 + len(variants(p)) for p in cards),
        "images": sum((1 if p.get("image") else 0) + sum(1 for x in variants(p) if x.get("image"))
                      for p in cards),
        "protected": sum(1 for p in cards if p.get("nsfw")),
    }


CARD_WIDTH_MIN, CARD_WIDTH_MAX = 180, 480   # px; must match the UI slider


def _clean_ui_prefs(raw):
    """Whitelist display preferences. bool is a subclass of int in Python, so
    cardWidth rejects it explicitly; out-of-range widths are clamped, not dropped."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    if isinstance(raw.get("imageCards"), bool):
        out["imageCards"] = raw["imageCards"]
    w = raw.get("cardWidth")
    if isinstance(w, (int, float)) and not isinstance(w, bool) and w == w:
        out["cardWidth"] = int(min(CARD_WIDTH_MAX, max(CARD_WIDTH_MIN, round(w))))
    return out


WINDOW_MIN = (900, 600)     # must match create_window(min_size=...)
WINDOW_MAX = (10000, 10000) # sanity cap against a corrupt settings file


def _clean_window(raw, default=(1280, 860)):
    """Validated launch geometry from settings.window: (width, height, maximized).
    Bad or missing values fall back to defaults; sizes are clamped to the
    window's own min_size so a stored tiny size can never trap the user."""
    raw = raw if isinstance(raw, dict) else {}

    def num(k, d, lo, hi):
        v = raw.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
            return d
        return int(min(hi, max(lo, round(v))))
    w = num("width", default[0], WINDOW_MIN[0], WINDOW_MAX[0])
    h = num("height", default[1], WINDOW_MIN[1], WINDOW_MAX[1])
    return w, h, raw.get("maximized") is True


class WindowTracker:
    """Remembers the window's normal size and whether it was maximised, so the
    next launch opens the same way (v5.8).

    Only STATE is tracked from events. Sizes are not: pywebview runs each event
    handler on its own thread, so `resized` and `maximized` can arrive in either
    order, and a resize-tracking scheme would sometimes record the maximised
    size as the normal one. Instead the real size is read once, at close
    (`closing` runs synchronously on the UI thread while the form is alive):
      * normal at close    -> save that size, maximized False
      * maximised at close -> save maximized True, keep the previous normal size
      * minimised at close -> keep the previous size and flag (closing from the taskbar)
    Cost: a normal-size change made during a session that ENDS maximised is not
    remembered. One settings write per session, never the vault.

    WinForms transitions: Normal->Max fires maximized; Max->Normal and Min->Normal
    fire restored; Min->Max fires maximized; Max->Min / Normal->Min fire minimized.
    """

    def __init__(self, width, height, maximized=False):
        self.size = (width, height)          # last known NORMAL size
        self.maximized = maximized
        self.minimized = False

    def on_maximized(self):
        self.maximized, self.minimized = True, False

    def on_minimized(self):
        self.minimized = True                # keep `maximized`: Max->Min->Max is common

    def on_restored(self):
        self.maximized = self.minimized = False

    def at_close(self, width, height):
        """Settings to write, given the size the window reports at close."""
        if not self.minimized and not self.maximized:
            w, h, _ = _clean_window({"width": width, "height": height}, self.size)
            self.size = (w, h)
        return {"width": self.size[0], "height": self.size[1], "maximized": self.maximized}


class Api:
    """Everything JavaScript can call. Keep the surface small and explicit."""

    def __init__(self):
        self.settings = Settings(SETTINGS_FILE)
        vp = self.settings.get("vault_path") or str(DEFAULT_VAULT)
        self.vault = Vault(vp)
        self._heartbeat = None
        self._stop = threading.Event()

    # ---------------------------------------------------------------- session
    def status(self):
        lock = self.vault.lock.read()
        return {
            "ok": True,
            "appVersion": APP_VERSION,
            "appBuild": APP_BUILD,
            "vaultPath": str(self.vault.path),
            "exists": self.vault.exists,
            "unlocked": self.vault.unlocked,
            "ui": self._ui_prefs(),
            "lock": None if lock is None else {
                "host": lock.host, "pid": lock.pid, "stale": lock.stale,
                "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(lock.since)),
            },
        }

    def _lift_expanded(self, db):
        """Move rollup open/closed state out of the vault and into settings.

        Older vaults (and browser backups) carry `expandedCategories`. Seed the
        settings copy from it the first time, then drop it from the db so the
        next ordinary save writes it away — no extra vault write just to migrate.
        """
        legacy = db.pop("expandedCategories", None)
        if legacy and not self.settings.get("expanded"):
            self.settings.set("expanded", legacy)
        return self.settings.get("expanded") or {}

    def set_expanded(self, expanded_json):
        """Called when a rollup is opened or closed. Writes ~200 bytes of JSON
        instead of re-encrypting and rotating the entire vault."""
        try:
            self.settings.set("expanded", json.loads(expanded_json))
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # Display preferences: UI state, so settings file, never the vault. Only
    # known keys passing their check are kept — the file holds no content.
    UI_PREFS = {"imageCards": bool, "cardWidth": int}

    def _ui_prefs(self):
        return _clean_ui_prefs(self.settings.get("ui") or {})

    def set_ui(self, prefs_json):
        try:
            raw = json.loads(prefs_json)
            if not isinstance(raw, dict):
                raise ValueError("expected an object")
            self.settings.set("ui", _clean_ui_prefs(raw))
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def clipboard_text(self):
        """Read clipboard text for the UI's right-click Paste.

        WebView2's navigator.clipboard.readText() is permission-gated, so the
        Win32 clipboard is read directly via ctypes — no extra dependency, no
        network. Other platforms report failure and the UI falls back."""
        if not sys.platform.startswith("win"):
            return {"ok": False, "error": "clipboard read is Windows-only"}
        import ctypes
        from ctypes import wintypes
        CF_UNICODETEXT = 13
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        # another app can hold the clipboard briefly; retry a few times
        for _ in range(5):
            if user32.OpenClipboard(None):
                break
            time.sleep(0.02)
        else:
            return {"ok": False, "error": "clipboard busy"}
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return {"ok": True, "text": ""}
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return {"ok": False, "error": "could not lock clipboard data"}
            try:
                text = ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
        return {"ok": True, "text": text}

    def create_vault(self, pin):
        try:
            db = self.vault.create(pin, _seed_db())
        except VaultError as e:
            return {"ok": False, "error": str(e)}
        self._begin_session()
        return {"ok": True, "db": db, "expanded": self._lift_expanded(db)}

    def unlock(self, pin):
        try:
            db = self.vault.unlock(pin)
        except BadPin as e:
            return {"ok": False, "error": str(e), "badPin": True}
        except (VaultError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        self._begin_session()
        return {"ok": True, "db": db, "expanded": self._lift_expanded(db),
                "upgraded": self.vault.last_upgrade}

    def lock(self):
        self.vault.lock_session()
        self._end_session()
        return {"ok": True}

    def change_pin(self, new_pin, db_json):
        try:
            self.vault.change_pin(new_pin, json.loads(db_json))
        except (VaultError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # ---------------------------------------------------------------- saving
    def save(self, db_json):
        """The one call the UI's honesty contract rests on."""
        try:
            n = self.vault.save(json.loads(db_json))
        except (VaultError, OSError, ValueError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "bytes": n}

    # ---------------------------------------------------------------- backups
    def export_backup(self, encrypted, db_json):
        kind = "backup"
        start = self.settings.resolve_start_dir(kind)
        stamp = time.strftime("%Y-%m-%d")
        name = f"promptvault-{'encrypted-' if encrypted else ''}{stamp}.json.gz"
        dest = self._save_dialog(name, start)
        if not dest:
            return {"ok": False, "cancelled": True}
        try:
            db = json.loads(db_json)
            if encrypted:
                self.vault.export_encrypted(dest, db)
            else:
                self.vault.export_plain(dest, db)
        except (VaultError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        self.settings.set("last_backup_dir", str(Path(dest).parent))
        return {"ok": True, "path": str(dest)}

    def pick_import(self):
        start = self.settings.resolve_start_dir("import")
        sel = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG, directory=start, allow_multiple=False,
            file_types=("Prompt Vault backup (*.gz;*.json;*.vault)", "All files (*.*)"))
        if not sel:
            return {"ok": False, "cancelled": True}
        path = sel[0]
        self.settings.set("last_import_dir", str(Path(path).parent))
        # Report whether a PIN will be needed before asking the user for one.
        try:
            from vault import read_maybe_gzip, is_encrypted_wrapper
            obj = json.loads(read_maybe_gzip(Path(path)).decode("utf-8"))
        except (OSError, ValueError) as e:
            return {"ok": False, "error": f"Could not read that file: {e}"}
        return {"ok": True, "path": path, "encrypted": is_encrypted_wrapper(obj)}

    def import_backup(self, path, pin=None, legacy_pin=None):
        """Two different PINs can be in play when importing from the browser
        build: one unlocking the backup FILE, and one that encrypted individual
        protected prompts inside it. For an encrypted browser backup they are
        the same key and the user is only asked once; for a plain backup
        containing protected prompts, only the second is needed. The UI is told
        which is missing rather than being left to guess."""
        try:
            rep = self.vault.inspect_import(path, pin)
        except BadPin:
            return {"ok": False, "badPin": True, "need": "filePin",
                    "error": "Incorrect PIN for this backup file."}
        except (VaultError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}

        if rep.get("needsFilePin"):
            return {"ok": False, "need": "filePin"}
        if rep.get("needsPin") and not rep.get("samePin") and legacy_pin is None:
            return {"ok": False, "need": "legacyPin", "info": rep}

        try:
            db = self.vault.read_import(path, pin, legacy_pin)
        except BadPin as e:
            return {"ok": False, "badPin": True,
                    "need": "legacyPin" if rep.get("needsPin") and not rep.get("samePin") else "filePin",
                    "error": str(e)}
        except (VaultError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        # An import replaces the library, so it must land on disk before the UI
        # claims anything — same rule as every other write.
        try:
            self.vault.save(db)
        except (VaultError, OSError) as e:
            return {"ok": False, "error": f"Imported but NOT saved: {e}", "db": db}
        return {"ok": True, "db": db, "expanded": self._lift_expanded(db),
                "migrated": rep.get("isLegacy", False),
                "counts": count_library(db)}

    # ---------------------------------------------------------------- settings
    def get_settings(self):
        return {"ok": True, "settings": self.settings.data}

    def choose_default_dir(self, kind):
        """kind: 'backup' | 'import'."""
        sel = webview.windows[0].create_file_dialog(
            webview.FOLDER_DIALOG, directory=self.settings.resolve_start_dir(kind))
        if not sel:
            return {"ok": False, "cancelled": True}
        self.settings.set(f"{kind}_dir", sel[0])
        return {"ok": True, "path": sel[0]}

    def clear_default_dir(self, kind):
        self.settings.set(f"{kind}_dir", "")
        return {"ok": True}

    def choose_vault_path(self):
        """Point at a different vault file. Takes effect on restart — swapping
        a live vault under an unlocked session is a good way to lose data."""
        sel = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG, directory=str(self.vault.path.parent),
            save_filename=self.vault.path.name)
        if not sel:
            return {"ok": False, "cancelled": True}
        p = sel if isinstance(sel, str) else sel[0]
        self.settings.set("vault_path", p)
        return {"ok": True, "path": p, "restart": True}

    def storage_info(self):
        s = self.vault.stats()
        s["ok"] = True
        s["settings"] = self.settings.data
        s["generationsKept"] = GENERATIONS
        return s

    def reveal_vault(self):
        """Open the containing folder. Uses the OS file manager, not a browser."""
        import subprocess
        p = self.vault.path.parent
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    # ---------------------------------------------------------------- internals
    def _save_dialog(self, filename, directory):
        sel = webview.windows[0].create_file_dialog(
            webview.SAVE_DIALOG, directory=directory or "", save_filename=filename)
        if not sel:
            return None
        return sel if isinstance(sel, str) else sel[0]

    def _begin_session(self):
        self.vault.lock.acquire()
        if self._heartbeat is None:
            self._stop.clear()
            self._heartbeat = threading.Thread(target=self._beat, daemon=True)
            self._heartbeat.start()

    def _beat(self):
        while not self._stop.wait(120):
            if self.vault.unlocked:
                self.vault.lock.touch()

    def _end_session(self):
        self._stop.set()
        self._heartbeat = None
        self.vault.lock.release()

    def shutdown(self):
        self._end_session()
        return {"ok": True}


def main():
    api = Api()
    width, height, maximized = _clean_window(api.settings.get("window", {}))
    win = webview.create_window(
        f"Prompt Vault {APP_VERSION}",
        str(UI_FILE),
        js_api=api,
        width=width,
        height=height,
        min_size=WINDOW_MIN,
        maximized=maximized,
        text_select=True,
    )
    tracker = WindowTracker(width, height, maximized)

    def on_closing():
        # Window geometry is UI state: settings file only, never the vault.
        # Never let this block closing: any failure just skips the write.
        try:
            api.settings.set("window", tracker.at_close(win.width, win.height))
        except Exception:
            pass

    def on_closed():
        api.shutdown()

    win.events.maximized += tracker.on_maximized
    win.events.minimized += tracker.on_minimized
    win.events.restored += tracker.on_restored
    win.events.closing += on_closing
    win.events.closed += on_closed
    # debug=False keeps devtools off in normal use; pass --debug to enable.
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
