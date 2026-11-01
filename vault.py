"""Prompt Vault — encrypted single-file vault storage.

The on-disk vault is byte-identical in format to the browser build's *encrypted
backup*: gzip( JSON( {promptVaultEncryptedBackup:1, salt, iterations, gz, iv, ct} ) ).
That is deliberate and load-bearing — it means

  * this app can open any encrypted backup the browser build produced, and
  * the browser build can import this app's live vault file directly.

Crypto, matching the browser exactly:
    PBKDF2-HMAC-SHA256, 600,000 iterations (stored per file), 16-byte salt -> 32-byte key
    AES-256-GCM, 12-byte IV, no AAD
    payload is gzipped JSON before encryption

Nothing here logs or transmits anything. No sockets are opened by this module.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

# OWASP's current floor for PBKDF2-HMAC-SHA256. Raised from the browser build's
# 150,000, which was inherited from an era of weaker guidance. The count travels
# in every file's wrapper, so older vaults still open — and are transparently
# re-encrypted at the new count on first unlock (see Vault.unlock).
PBKDF2_ITERATIONS = 600_000
PIN_MAGIC = "PROMPTVAULT-PIN-OK-v1"   # browser build's verifier plaintext
SALT_BYTES = 16
IV_BYTES = 12
KEY_BYTES = 32
GENERATIONS = 3          # promptvault.vault.1 .. .3, rotated oldest-out on each save
LOCK_STALE_SECONDS = 600


class VaultError(Exception):
    """Any recoverable storage failure. The message is shown to the user."""


class BadPin(VaultError):
    pass


# --------------------------------------------------------------------------- crypto
def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def derive_key(pin: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """PBKDF2-SHA256 over the UTF-8 PIN. Identical to the browser's deriveKey()."""
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, iterations=iterations
    ).derive(pin.encode("utf-8"))


def encrypt_payload(obj, key: bytes, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> dict:
    """Object -> the wrapper dict the browser build also writes."""
    payload = gzip.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"))
    iv = os.urandom(IV_BYTES)
    ct = AESGCM(key).encrypt(iv, payload, None)
    return {
        "promptVaultEncryptedBackup": 1,
        "salt": _b64e(salt),
        "iterations": iterations,
        "gz": True,
        "iv": _b64e(iv),
        "ct": _b64e(ct),
    }


def decrypt_payload(wrapper: dict, key: bytes):
    """Wrapper dict -> object. Raises BadPin when the key doesn't fit."""
    try:
        plain = AESGCM(key).decrypt(_b64d(wrapper["iv"]), _b64d(wrapper["ct"]), None)
    except InvalidTag:
        raise BadPin("Incorrect PIN, or the file is damaged.")
    except (KeyError, ValueError) as e:
        raise VaultError(f"Not a readable vault file: {e}")
    if wrapper.get("gz"):
        plain = gzip.decompress(plain)
    return json.loads(plain.decode("utf-8"))


def is_encrypted_wrapper(obj) -> bool:
    return isinstance(obj, dict) and obj.get("promptVaultEncryptedBackup") == 1


# ------------------------------------------------------- browser-backup migration
def decrypt_blob(enc: dict, key: bytes):
    """One of the browser build's per-item {iv,ct} blobs. Not gzipped, unlike
    the whole-file payload."""
    try:
        plain = AESGCM(key).decrypt(_b64d(enc["iv"]), _b64d(enc["ct"]), None)
    except InvalidTag:
        raise BadPin("Incorrect PIN for this backup's protected content.")
    except (KeyError, ValueError) as e:
        raise VaultError(f"Damaged encrypted block: {e}")
    return json.loads(plain.decode("utf-8"))


def legacy_report(db: dict) -> dict:
    """What a browser backup contains that needs translating, so the caller can
    ask for a PIN only when one is actually required."""
    prompts = db.get("prompts") or []
    images = db.get("images") or {}
    enc_prompts = sum(1 for p in prompts if isinstance(p, dict) and p.get("enc"))
    enc_images = sum(1 for r in images.values() if isinstance(r, dict) and r.get("enc"))
    return {
        "isLegacy": bool(images) or enc_prompts > 0 or "backupVersion" in db,
        "imageMap": len(images),
        "encryptedPrompts": enc_prompts,
        "encryptedImages": enc_images,
        "needsPin": enc_prompts > 0 or enc_images > 0,
        "hasSecurity": bool(db.get("security")),
    }


def verify_legacy_pin(db: dict, pin: str) -> bytes:
    """Derive and check the browser PIN against db.security.verifier, so a wrong
    PIN is reported as a wrong PIN rather than as corrupt content."""
    sec = db.get("security") or {}
    if not sec.get("salt"):
        raise VaultError("This backup has protected prompts but no key parameters; "
                         "it cannot be decrypted.")
    key = derive_key(pin, _b64d(sec["salt"]), int(sec.get("iterations", PBKDF2_ITERATIONS)))
    ver = sec.get("verifier")
    if ver:
        if decrypt_blob(ver, key).get("m") != PIN_MAGIC:
            raise BadPin("Incorrect PIN for this backup.")
    return key


def migrate_browser_backup(db: dict, key: bytes | None = None) -> dict:
    """Translate a browser-build backup into the desktop shape, in place.

    The browser build stores protected prompt text as a per-prompt AES-GCM blob
    and keeps images in a top-level `images` map. The desktop vault encrypts the
    whole file instead, so both have to be unpacked on the way in — otherwise
    protected prompts import blank and no prompt gets its image.
    """
    images = db.pop("images", None) or {}
    db.pop("backupVersion", None)

    for p in db.get("prompts") or []:
        if not isinstance(p, dict):
            continue

        # 1. protected prompt text
        enc = p.pop("enc", None)
        if enc:
            if key is None:
                raise BadPin("This backup contains protected prompts; a PIN is required.")
            fields = decrypt_blob(enc, key)
            for k, v in fields.items():
                p.setdefault(k, v)
            # an image inside `enc` means a pre-v3.16 backup
            if isinstance(fields.get("image"), dict):
                p["image"] = fields["image"]

        # 2. image from the top-level map (v1-era inline images are left alone)
        rec = images.get(p.get("id"))
        if isinstance(rec, dict) and not p.get("image"):
            if rec.get("enc"):
                if key is None:
                    raise BadPin("This backup contains protected images; a PIN is required.")
                p["image"] = decrypt_blob(rec["blob"], key)
            elif rec.get("data"):
                p["image"] = {"data": rec["data"], "w": rec.get("w"), "h": rec.get("h")}

        # defaults the desktop UI expects
        p.setdefault("nsfw", False)
        p.setdefault("categoryId", None)
        p.setdefault("tags", [])
        for f in ("promptText", "negativePrompt", "seed", "comments"):
            p.setdefault(f, "")

    # The browser's PIN parameters are meaningless here: the vault passphrase
    # protects everything now. Carrying them over would imply otherwise.
    db.pop("security", None)
    db.setdefault("libCategories", [])
    db.setdefault("expandedCategories", {})
    return db


# --------------------------------------------------------------------------- file io
def read_maybe_gzip(path: Path) -> bytes:
    """Both the browser and this app write .gz; accept plain JSON too."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file in the SAME directory, fsync, then replace.

    os.replace is atomic on both Windows and POSIX when source and destination
    share a filesystem — hence the same-directory temp file. A half-written
    vault is unrecoverable (a truncated AES-GCM ciphertext fails its tag), so
    this path must never leave a partial file where the real one was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def rotate_generations(path: Path, keep: int = GENERATIONS) -> None:
    """Shift vault.N -> vault.N+1 and copy the current vault to vault.1.

    Cheap insurance against a corrupted write or a bad import: the previous
    `keep` states stay recoverable. Failures here are swallowed — losing a
    backup generation must never block the real save.
    """
    if not path.exists():
        return
    try:
        for n in range(keep - 1, 0, -1):
            src, dst = Path(f"{path}.{n}"), Path(f"{path}.{n + 1}")
            if src.exists():
                os.replace(src, dst)
        write_atomic(Path(f"{path}.1"), path.read_bytes())
    except OSError:
        pass


# --------------------------------------------------------------------------- lock
@dataclass
class LockInfo:
    host: str
    pid: int
    since: float
    stale: bool


class VaultLock:
    """Advisory single-writer lock beside the vault.

    Advisory on purpose. A hard lock over SMB is unreliable, and the realistic
    risk here is one person opening the same vault twice, not a race. The app
    warns and lets the user decide.
    """

    def __init__(self, vault_path: Path):
        self.path = Path(str(vault_path) + ".lock")

    def read(self) -> LockInfo | None:
        try:
            d = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        age = time.time() - d.get("since", 0)
        return LockInfo(d.get("host", "?"), d.get("pid", 0), d.get("since", 0),
                        age > LOCK_STALE_SECONDS)

    def acquire(self) -> None:
        payload = {"host": socket.gethostname(), "pid": os.getpid(), "since": time.time()}
        try:
            write_atomic(self.path, json.dumps(payload).encode("utf-8"))
        except OSError:
            pass

    def touch(self) -> None:
        self.acquire()

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- vault
# Note what is NOT here: expandedCategories. Which rollups are open is UI state,
# not data, and keeping it in the vault meant merely browsing the Library wrote
# (and rotated) the whole encrypted file. It lives in the settings file instead —
# see Settings.expanded and app.Api._lift_expanded.
EMPTY_DB = {
    "version": 1,
    "dataVersion": 1,
    "prompts": [],
    "libCategories": [],
}


class Vault:
    """Open/save a single encrypted vault file. The key never leaves this object."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._key: bytes | None = None
        self.last_upgrade: dict | None = None   # set when unlock() re-encrypted at a higher count
        self._salt: bytes | None = None
        self._iterations = PBKDF2_ITERATIONS
        self.lock = VaultLock(self.path)

    # -- state
    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    def lock_session(self) -> None:
        self._key = None
        self._salt = None

    # -- lifecycle
    def create(self, pin: str, db=None) -> dict:
        if self.exists:
            raise VaultError("A vault already exists at this path.")
        if len(pin) < 4:
            raise VaultError("PIN must be at least 4 characters.")
        self._salt = os.urandom(SALT_BYTES)
        self._key = derive_key(pin, self._salt, self._iterations)
        data = json.loads(json.dumps(db if db is not None else EMPTY_DB))
        self.save(data)
        return data

    def unlock(self, pin: str) -> dict:
        if not self.exists:
            raise VaultError("No vault file at this path.")
        wrapper = json.loads(read_maybe_gzip(self.path).decode("utf-8"))
        if not is_encrypted_wrapper(wrapper):
            raise VaultError("That file is not an encrypted vault.")
        salt = _b64d(wrapper["salt"])
        iterations = int(wrapper.get("iterations", PBKDF2_ITERATIONS))
        key = derive_key(pin, salt, iterations)
        db = decrypt_payload(wrapper, key)      # raises BadPin on a wrong PIN
        self._key, self._salt, self._iterations = key, salt, iterations
        self.last_upgrade = None
        if iterations < PBKDF2_ITERATIONS:
            self._upgrade_kdf(pin, db, iterations)
        return db

    def _upgrade_kdf(self, pin: str, db, old_iterations: int) -> None:
        """Re-encrypt an older vault at the current iteration count.

        Runs once, on the first unlock after the constant is raised, because the
        count is baked into each file — an old vault would otherwise keep its
        weaker derivation forever. If the rewrite fails for any reason the old
        key is restored so the session still works; a failed hardening step must
        never cost the user access to their vault.
        """
        prev = (self._key, self._salt, self._iterations)
        try:
            self._salt = os.urandom(SALT_BYTES)
            self._iterations = PBKDF2_ITERATIONS
            self._key = derive_key(pin, self._salt, self._iterations)
            self.save(db)
            self.last_upgrade = {"from": old_iterations, "to": PBKDF2_ITERATIONS}
        except (VaultError, OSError):
            self._key, self._salt, self._iterations = prev
            self.last_upgrade = None

    def save(self, db) -> int:
        """Encrypt and write. Returns bytes written. Raises on any failure —
        the caller must surface that, never swallow it."""
        if not self.unlocked:
            raise VaultError("Vault is locked; cannot save.")
        wrapper = encrypt_payload(db, self._key, self._salt, self._iterations)
        blob = gzip.compress(json.dumps(wrapper, separators=(",", ":")).encode("utf-8"))
        rotate_generations(self.path)
        write_atomic(self.path, blob)
        return len(blob)

    def change_pin(self, new_pin: str, db) -> None:
        if not self.unlocked:
            raise VaultError("Unlock before changing the PIN.")
        if len(new_pin) < 4:
            raise VaultError("PIN must be at least 4 characters.")
        self._salt = os.urandom(SALT_BYTES)
        self._iterations = PBKDF2_ITERATIONS
        self._key = derive_key(new_pin, self._salt, self._iterations)
        self.save(db)

    # -- backups
    def export_encrypted(self, dest: str | Path, db) -> Path:
        """Same format as the live vault, so it re-imports anywhere."""
        if not self.unlocked:
            raise VaultError("Unlock before exporting.")
        wrapper = encrypt_payload(db, self._key, self._salt, self._iterations)
        dest = Path(dest)
        write_atomic(dest, gzip.compress(json.dumps(wrapper, separators=(",", ":")).encode("utf-8")))
        return dest

    def export_plain(self, dest: str | Path, db) -> Path:
        """Readable by anyone who opens it. Offered because the browser build
        offers it and backups should stay interchangeable — the UI warns."""
        dest = Path(dest)
        write_atomic(dest, gzip.compress(json.dumps(db, indent=2).encode("utf-8")))
        return dest

    def inspect_import(self, src: str | Path, pin: str | None = None) -> dict:
        """Open a backup far enough to say what it is and what it still needs."""
        obj = json.loads(read_maybe_gzip(Path(src)).decode("utf-8"))
        wrapper_salt = None
        if is_encrypted_wrapper(obj):
            if pin is None:
                return {"fileEncrypted": True, "needsFilePin": True}
            wrapper_salt = obj["salt"]
            key = derive_key(pin, _b64d(obj["salt"]), int(obj.get("iterations", PBKDF2_ITERATIONS)))
            obj = decrypt_payload(obj, key)
        rep = legacy_report(obj if isinstance(obj, dict) else {})
        rep["fileEncrypted"] = wrapper_salt is not None
        # When the file PIN also protects the contents (the usual case for an
        # encrypted browser backup) no second PIN is needed.
        sec = (obj or {}).get("security") or {}
        rep["samePin"] = bool(wrapper_salt and sec.get("salt") == wrapper_salt)
        return rep

    def read_import(self, src: str | Path, pin: str | None = None,
                    legacy_pin: str | None = None) -> dict:
        """Load a backup of either format. Returns the db object.

        Encrypted backups may carry a *different* PIN from this vault (they
        often come from another machine), so the caller supplies one and the
        vault's own key is never assumed to fit.
        """
        obj = json.loads(read_maybe_gzip(Path(src)).decode("utf-8"))
        file_key = None
        wrapper_salt = None
        if is_encrypted_wrapper(obj):
            if pin is None:
                raise BadPin("This backup is encrypted; a PIN is required.")
            wrapper_salt = obj["salt"]
            file_key = derive_key(pin, _b64d(wrapper_salt),
                                  int(obj.get("iterations", PBKDF2_ITERATIONS)))
            obj = decrypt_payload(obj, file_key)
        if not isinstance(obj, dict) or "prompts" not in obj:
            raise VaultError("That file does not look like a Prompt Vault backup.")

        rep = legacy_report(obj)
        if rep["isLegacy"]:
            key = None
            if rep["needsPin"]:
                sec = obj.get("security") or {}
                if file_key is not None and sec.get("salt") == wrapper_salt:
                    key = file_key          # the file PIN is the content PIN
                elif legacy_pin is not None:
                    key = verify_legacy_pin(obj, legacy_pin)
                else:
                    raise BadPin("This backup's protected prompts need the PIN they "
                                 "were created with.")
            obj = migrate_browser_backup(obj, key)
        return obj

    # -- reporting
    def stats(self) -> dict:
        gens = []
        for n in range(1, GENERATIONS + 1):
            p = Path(f"{self.path}.{n}")
            if p.exists():
                gens.append({"n": n, "bytes": p.stat().st_size,
                             "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))})
        return {
            "path": str(self.path),
            "exists": self.exists,
            "bytes": self.path.stat().st_size if self.exists else 0,
            "mtime": (time.strftime("%Y-%m-%d %H:%M", time.localtime(self.path.stat().st_mtime))
                      if self.exists else None),
            "iterations": self._iterations,
            "generations": gens,
        }


# --------------------------------------------------------------------------- settings
DEFAULT_SETTINGS = {
    "vault_path": "",          # blank = promptvault.vault beside the app
    "backup_dir": "",          # default target for exports; blank = ask each time
    "import_dir": "",          # default source for imports
    "last_backup_dir": "",     # remembered from the last export
    "last_import_dir": "",     # remembered from the last import
    "expanded": {},            # libCategoryId|'__uncat__' -> bool. UI state, deliberately
                               # NOT in the vault: see EMPTY_DB. Holds opaque ids only,
                               # never category names or any prompt content.
    "ui": {"imageCards": False, "cardWidth": 230},  # display prefs (v5.5+). UI state, not data.
    "window": {"width": 1280, "height": 860, "maximized": False},  # written on close (v5.8)
}


class Settings:
    """Plain JSON beside the app, so the whole folder stays portable."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        try:
            disk = json.loads(self.path.read_text("utf-8"))
            if isinstance(disk, dict):
                self.data.update(disk)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            write_atomic(self.path, json.dumps(self.data, indent=2).encode("utf-8"))
        except OSError:
            pass

    def get(self, k, default=""):
        return self.data.get(k, default)

    def set(self, k, v) -> None:
        self.data[k] = v
        self.save()

    def resolve_start_dir(self, kind: str) -> str:
        """Default dir wins if set, else the last used one, else empty (OS default)."""
        return (self.get(f"{kind}_dir") or self.get(f"last_{kind}_dir") or "")
