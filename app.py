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
import os
import sys
import threading
import time
from pathlib import Path

import webview

from vault import (
    Settings, Vault, VaultError, BadPin, EMPTY_DB, GENERATIONS,
    VAULT_EXT, MAX_PATH_CHARS, vault_file_name, vault_name,
    read_maybe_gzip, is_encrypted_wrapper,
)

APP_VERSION = "6.8"
APP_BUILD = "2026-11-03"

# Beside the app whether we're a script or a PyInstaller one-file bundle.
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
HOME_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent

UI_FILE = BASE_DIR / "ui" / "index.html"
SETTINGS_FILE = HOME_DIR / "promptvault-settings.json"
DEFAULT_VAULT = HOME_DIR / "promptvault.vault"
VAULTS_DIR = HOME_DIR / "vaults"      # where new vaults go unless another folder is chosen

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


def _vkey(path):
    """Comparison key for a vault path: absolute, and case-folded on Windows,
    where D:\\Vaults\\A.vault and d:\\vaults\\a.vault are the same file."""
    return os.path.normcase(os.path.abspath(str(path)))


def _when(t):
    return None if t is None else time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _clean_entries(raw):
    """The vault list from settings: dicts with a string path and numeric (or
    null) opened/backup times, de-duplicated by path. Anything else is dropped."""
    out, seen = [], set()
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not e["path"].strip():
            continue
        k = _vkey(e["path"])
        if k in seen:
            continue
        seen.add(k)
        num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        out.append({"path": e["path"], "opened": num(e.get("opened")), "backup": num(e.get("backup"))})
    return out


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
        self.vault = Vault(self._migrate_vaults())
        self._heartbeat = None
        self._stop = threading.Event()

    # ---------------------------------------------------------------- vault list
    # Several vaults, one open at a time. The list lives in the settings file as
    # paths and timestamps only; a vault's name is its file name, so the list
    # exposes nothing the folder listing doesn't already show. Each vault keeps
    # its own passphrase, generations and lock file beside it.
    def _migrate_vaults(self):
        """Launch path, and a one-time move of pre-v6.7 settings: the single
        `vault_path` becomes the first list entry, and the flat `expanded` map
        becomes that vault's entry in `expanded_by_vault`. Writes settings only
        when something actually changed."""
        s = self.settings
        before = json.dumps(s.data, sort_keys=True)
        entries = _clean_entries(s.get("vaults"))
        launch = s.get("last_vault") or s.get("vault_path") or str(DEFAULT_VAULT)
        if not any(_vkey(e["path"]) == _vkey(launch) for e in entries):
            entries.append({"path": str(launch), "opened": None, "backup": None})
        flat = s.get("expanded")
        if isinstance(flat, dict) and flat:
            ebv = dict(s.get("expanded_by_vault") or {})
            ebv.setdefault(_vkey(launch), flat)
            s.data["expanded_by_vault"] = ebv
            s.data["expanded"] = {}
        s.data["vaults"] = entries
        s.data["last_vault"] = str(launch)
        s.data["vault_path"] = ""
        if json.dumps(s.data, sort_keys=True) != before:
            s.save()
        return launch

    def _entries(self):
        return _clean_entries(self.settings.get("vaults"))

    def _find(self, path):
        k = _vkey(path)
        return next((e for e in self._entries() if _vkey(e["path"]) == k), None)

    def _put_entry(self, path, **fields):
        """Add or update one list entry, then write settings once."""
        entries, k = self._entries(), _vkey(path)
        for e in entries:
            if _vkey(e["path"]) == k:
                e.update(fields)
                break
        else:
            entries.append({"path": str(path), "opened": None, "backup": None, **fields})
        self.settings.set("vaults", entries)

    def _vault_list(self):
        """The list for the UI, most recently opened first. `known` = opened at
        least once, so a missing file reads as missing, not as 'create one here'."""
        cur, out = _vkey(self.vault.path), []
        for e in self._entries():
            p = Path(e["path"])
            try:
                exists = p.is_file()
                mtime = p.stat().st_mtime if exists else None
            except OSError:
                exists, mtime = False, None
            out.append({
                "path": e["path"], "name": vault_name(p), "exists": exists,
                "current": _vkey(p) == cur, "known": e["opened"] is not None,
                "opened": _when(e["opened"]), "backup": _when(e["backup"]),
                "changedSinceBackup": bool(exists and (e["backup"] is None or mtime > e["backup"] + 1)),
                "_sort": e["opened"] or 0,
            })
        out.sort(key=lambda v: -v["_sort"])
        for v in out:
            del v["_sort"]
        return out

    def list_vaults(self):
        return {"ok": True, "vaults": self._vault_list()}

    def _title(self):
        return f"Prompt Vault {APP_VERSION} — {vault_name(self.vault.path)}"

    def _set_title(self):
        try:
            webview.windows[0].set_title(self._title())
        except Exception:
            pass      # cosmetic: never let the title cost a switch or an unlock

    def _opened(self):
        self._put_entry(self.vault.path, opened=time.time())
        self.settings.set("last_vault", str(self.vault.path))

    def select_vault(self, path):
        """Make another listed vault the current one, ready for its passphrase.
        Only while locked: the UI locks the open vault first, after its
        pre-switch checks, so an unlocked session is never swapped underneath."""
        if self.vault.unlocked:
            return {"ok": False, "error": "Lock the open vault before switching."}
        e = self._find(path)
        if not e:
            return {"ok": False, "error": "That vault is not in the list."}
        self.vault = Vault(e["path"])
        self.settings.set("last_vault", e["path"])
        self._set_title()
        return self.status()

    @staticmethod
    def _check_vault_file(p):
        """None if p is an encrypted vault file, else the reason it isn't."""
        if p.suffix.lower() != VAULT_EXT:
            return ("Choose a .vault file. To use a backup as a vault, create a new "
                    "vault and import the backup into it.")
        try:
            obj = json.loads(read_maybe_gzip(p).decode("utf-8"))
        except (OSError, ValueError) as e:
            return f"Could not read that file: {e}"
        if not is_encrypted_wrapper(obj):
            return "That file is not an encrypted Prompt Vault."
        return None

    def add_vault_file(self):
        """Add an existing .vault file to the list. Allowed while a vault is
        open: it only lists the file, it opens nothing."""
        start = VAULTS_DIR if VAULTS_DIR.is_dir() else HOME_DIR
        sel = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG, directory=str(start), allow_multiple=False,
            file_types=("Prompt Vault (*.vault)", "All files (*.*)"))
        if not sel:
            return {"ok": False, "cancelled": True}
        p = Path(sel if isinstance(sel, str) else sel[0])
        why = self._check_vault_file(p)
        if why:
            return {"ok": False, "error": why}
        known = self._find(p)
        if not known:
            self._put_entry(p)
        return {"ok": True, "path": known["path"] if known else str(p), "name": vault_name(p),
                "already": bool(known), "vaults": self._vault_list()}

    def forget_vault(self, path):
        """Remove a vault from the list. The file and its copies stay on disk."""
        if _vkey(path) == _vkey(self.vault.path):
            return {"ok": False, "error": "That vault is the current one. Switch to another vault first."}
        k = _vkey(path)
        self.settings.data["vaults"] = [e for e in self._entries() if _vkey(e["path"]) != k]
        ebv = dict(self.settings.get("expanded_by_vault") or {})
        ebv.pop(k, None)
        self.settings.set("expanded_by_vault", ebv)
        return {"ok": True, "vaults": self._vault_list()}

    def choose_vault_folder(self):
        start = VAULTS_DIR if VAULTS_DIR.is_dir() else HOME_DIR
        sel = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG, directory=str(start))
        if not sel:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": sel if isinstance(sel, str) else sel[0]}

    # ---------------------------------------------------------------- session
    def status(self):
        lock = self.vault.lock.read()
        entry = self._find(self.vault.path)
        return {
            "ok": True,
            "appVersion": APP_VERSION,
            "appBuild": APP_BUILD,
            "vaultPath": str(self.vault.path),
            "vaultName": vault_name(self.vault.path),
            "vaultsDir": str(VAULTS_DIR),
            "exists": self.vault.exists,
            "known": bool(entry and entry["opened"] is not None),
            "vaults": self._vault_list(),
            "unlocked": self.vault.unlocked,
            "ui": self._ui_prefs(),
            "lock": None if lock is None else {
                "host": lock.host, "pid": lock.pid, "stale": lock.stale,
                "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(lock.since)),
            },
        }

    def _expanded_map(self):
        return dict(self.settings.get("expanded_by_vault") or {})

    def _lift_expanded(self, db):
        """Move rollup open/closed state out of the vault and into settings.

        Older vaults (and browser backups) carry `expandedCategories`. Seed the
        settings copy from it the first time, then drop it from the db so the
        next ordinary save writes it away — no extra vault write just to migrate.
        Kept per vault (v6.7): category ids repeat between vaults.
        """
        legacy = db.pop("expandedCategories", None)
        ebv, k = self._expanded_map(), _vkey(self.vault.path)
        if legacy and not ebv.get(k):
            ebv[k] = legacy
            self.settings.set("expanded_by_vault", ebv)
        return ebv.get(k) or {}

    def set_expanded(self, expanded_json):
        """Called when a rollup is opened or closed. Writes ~200 bytes of JSON
        instead of re-encrypting and rotating the entire vault."""
        try:
            ebv = self._expanded_map()
            ebv[_vkey(self.vault.path)] = json.loads(expanded_json)
            self.settings.set("expanded_by_vault", ebv)
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

    def create_vault(self, pin, name=None, folder=None):
        """With no name: first run, creating the current vault with the demo
        card. With a name (v6.7): a new, empty vault <name>.vault in `folder`
        (default: the vaults folder beside the app), which becomes current."""
        if name is None:
            try:
                db = self.vault.create(pin, _seed_db())
            except VaultError as e:
                return {"ok": False, "error": str(e)}
        else:
            if self.vault.unlocked:
                return {"ok": False, "error": "Lock the open vault first."}
            try:
                fname = vault_file_name(name)
                stem = fname[:-len(VAULT_EXT)]
                if any(vault_name(e["path"]).lower() == stem.lower() for e in self._entries()):
                    raise VaultError(f"A vault called “{stem}” is already in your list.")
                d = Path(folder) if folder else VAULTS_DIR
                d.mkdir(parents=True, exist_ok=True)
                # case-insensitive on every OS, so a name that works here works on Windows
                if any(c.name.lower() == fname.lower() for c in d.iterdir()):
                    raise VaultError(f"{fname} already exists in that folder.")
                path = d / fname
                if len(str(path.resolve())) > MAX_PATH_CHARS:
                    raise VaultError("That folder's path is too long for Windows. Choose a shorter one.")
                v = Vault(path)
                db = v.create(pin, json.loads(json.dumps(EMPTY_DB)))
            except (VaultError, OSError) as e:
                return {"ok": False, "error": str(e)}
            self.vault = v
        self._opened()
        self._begin_session()
        self._set_title()
        return {"ok": True, "db": db, "expanded": self._lift_expanded(db),
                "vaultPath": str(self.vault.path), "vaultName": vault_name(self.vault.path)}

    def unlock(self, pin):
        try:
            db = self.vault.unlock(pin)
        except BadPin as e:
            return {"ok": False, "error": str(e), "badPin": True}
        except (VaultError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        self._opened()
        self._begin_session()
        self._set_title()
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
        # named after the vault, so two vaults backed up on one day don't collide
        name = f"{vault_name(self.vault.path)}-{'encrypted-' if encrypted else ''}{stamp}.json.gz"
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
        self._put_entry(self.vault.path, backup=time.time())
        return {"ok": True, "path": str(dest)}

    def switch_backup(self, db_json):
        """The backup offered before switching vaults: encrypted, straight into
        the backup folder (the default one, else the last one used) with no
        dialog, named <vault>-encrypted-<date>-<time>.json.gz and never
        overwriting. With no usable folder it says so, and the UI falls back to
        the ordinary export dialog."""
        d = self.settings.get("backup_dir") or self.settings.get("last_backup_dir")
        if not d or not Path(d).is_dir():
            return {"ok": False, "needDir": True}
        base = f"{vault_name(self.vault.path)}-encrypted-{time.strftime('%Y-%m-%d-%H%M')}"
        dest, n = Path(d) / f"{base}.json.gz", 2
        while dest.exists():
            dest, n = Path(d) / f"{base}-{n}.json.gz", n + 1
        try:
            self.vault.export_encrypted(dest, json.loads(db_json))
        except (VaultError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        self._put_entry(self.vault.path, backup=time.time())
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

    def _decrypt_import(self, path, pin=None, legacy_pin=None):
        """Two different PINs can be in play when importing from the browser
        build: one unlocking the backup FILE, and one that encrypted individual
        protected prompts inside it. For an encrypted browser backup they are
        the same key and the user is only asked once; for a plain backup
        containing protected prompts, only the second is needed. The UI is told
        which is missing rather than being left to guess.

        Returns (db, report, None) on success or (None, None, reply) where reply
        is the {"ok": False, ...} answer for the UI."""
        try:
            rep = self.vault.inspect_import(path, pin)
        except BadPin:
            return None, None, {"ok": False, "badPin": True, "need": "filePin",
                    "error": "Incorrect PIN for this backup file."}
        except (VaultError, OSError, ValueError) as e:
            return None, None, {"ok": False, "error": str(e)}

        if rep.get("needsFilePin"):
            return None, None, {"ok": False, "need": "filePin"}
        if rep.get("needsPin") and not rep.get("samePin") and legacy_pin is None:
            return None, None, {"ok": False, "need": "legacyPin", "info": rep}

        try:
            db = self.vault.read_import(path, pin, legacy_pin)
        except BadPin as e:
            return None, None, {"ok": False, "badPin": True,
                    "need": "legacyPin" if rep.get("needsPin") and not rep.get("samePin") else "filePin",
                    "error": str(e)}
        except (VaultError, OSError, ValueError) as e:
            return None, None, {"ok": False, "error": str(e)}
        return db, rep, None

    def import_backup(self, path, pin=None, legacy_pin=None):
        """REPLACE import: the backup becomes the whole library."""
        db, rep, err = self._decrypt_import(path, pin, legacy_pin)
        if err:
            return err
        # An import replaces the library, so it must land on disk before the UI
        # claims anything — same rule as every other write.
        try:
            self.vault.save(db)
        except (VaultError, OSError) as e:
            return {"ok": False, "error": f"Imported but NOT saved: {e}", "db": db}
        return {"ok": True, "db": db, "expanded": self._lift_expanded(db),
                "migrated": rep.get("isLegacy", False),
                "counts": count_library(db)}

    def read_backup(self, path, pin=None, legacy_pin=None):
        """MERGE import, step one: decrypt and migrate a backup exactly as
        import_backup does, but write NOTHING - not the vault, not settings. The
        UI compares it with the open library, asks about conflicts, and saves the
        merged result through the ordinary save() (under this vault's key; the
        backup's passphrase is only ever used to read the file)."""
        db, rep, err = self._decrypt_import(path, pin, legacy_pin)
        if err:
            return err
        db.pop("expandedCategories", None)   # UI state from the other vault: not ours
        return {"ok": True, "db": db, "migrated": rep.get("isLegacy", False),
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

    def storage_info(self):
        s = self.vault.stats()
        s["ok"] = True
        s["name"] = vault_name(self.vault.path)
        s["settings"] = self.settings.data
        s["generationsKept"] = GENERATIONS
        return s

    def reveal_vault(self, path=None):
        """Open the folder holding the current vault, or a listed one. Uses
        the OS file manager, not a browser."""
        import subprocess
        e = self._find(path) if path else None
        p = Path(e["path"]).parent if e else self.vault.path.parent
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
        api._title(),
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
