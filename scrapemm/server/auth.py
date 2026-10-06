"""Bearer-token authentication for the API and the web UI, with a role per key.

The root key comes from SCRAPEMM_API_KEY. If none is set the server generates one on
first start and keeps it in the config directory, so a deployment is never accidentally
wide open -- the key is printed at startup for the admin to copy into the UI.

Further keys are created in the web UI, each named and with a role:

* Root   -- the root key above; there is exactly one
* Admin  -- everything, including Secrets, Settings, Logs and the API keys
* Client -- retrieval, search and the views around them; nothing that configures the
  server or reveals its credentials

A key manages only keys of a lower role: Root the Admins and Clients, an Admin the
Clients. The server keeps only a hash of each key; the key itself is shown once, when
it is created.
"""

import hashlib
import json
import logging
import os
import secrets as secrets_module
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Depends, Header, HTTPException, Query, status

from scrapemm.common.paths import APP_NAME
from . import paths
from .paths import CONFIG_DIR

logger = logging.getLogger(APP_NAME)

API_KEY_PATH = CONFIG_DIR / "api_key"

_api_key: Optional[str] = None


def api_key() -> str:
    """The token this server accepts, generated on first use."""
    global _api_key
    if _api_key is not None:
        return _api_key

    if env_key := os.getenv("SCRAPEMM_API_KEY"):
        _api_key = env_key.strip()
        return _api_key

    try:
        stored = API_KEY_PATH.read_text(encoding="utf-8").strip()
        if stored:
            _api_key = stored
            return _api_key
    except OSError:
        pass

    _api_key = _generate()
    logger.warning("🔑 No SCRAPEMM_API_KEY was set, so a new API key was generated.")
    return _api_key


def log_api_key() -> None:
    """Prints the root key on every startup, so the admin can always find it in the
    server's output. Not on the web UI's Logs page, which Admins see too (see
    `logbuffer._redact()`)."""
    key = api_key()
    source = "from SCRAPEMM_API_KEY" if api_key_from_environment() else f"stored in {API_KEY_PATH}"
    logger.info(f"🔑 Root API key ({source}): {key}\n"
                f"   Enter it in the web UI, or set SCRAPEMM_API_KEY in your .env.")


def api_key_from_environment() -> bool:
    return bool(os.getenv("SCRAPEMM_API_KEY"))


def regenerate_api_key() -> str:
    """Replaces the token with a fresh one; the old one stops working immediately."""
    global _api_key
    if api_key_from_environment():
        raise RuntimeError("The API key is set by SCRAPEMM_API_KEY in the environment, "
                           "which would override a regenerated one on the next start. "
                           "Change it there instead, or remove it to manage the key here.")
    _api_key = _generate()
    logger.warning("🔑 The API key was regenerated. Clients using the old one are now "
                   "rejected.")
    return _api_key


def _generate() -> str:
    """A new token, persisted so that it survives restarts."""
    key = secrets_module.token_urlsafe(32)
    try:
        API_KEY_PATH.write_text(key, encoding="utf-8")
        os.chmod(API_KEY_PATH, 0o600)
    except OSError:
        logger.warning(f"Could not persist the API key to {API_KEY_PATH}; a new one "
                       f"will be generated on the next start.")
    return key


def _token_from(authorization: Optional[str], token: Optional[str]) -> Optional[str]:
    """Reads the token from the Authorization header, or from a query parameter -- the
    latter because a browser opening a WebSocket cannot set headers."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    return token


# --- Roles and keys -----------------------------------------------------------------

ROOT, ADMIN, CLIENT = "root", "admin", "client"
ROLES = (ROOT, ADMIN, CLIENT)
_RANK = {ROOT: 3, ADMIN: 2, CLIENT: 1}

ROOT_ID = "root"
TOKEN_PREFIX = "smm_"  # Makes a leaked key recognizable as scrapeMM's
MAX_NAME_LENGTH = 64
LAST_USED_RESOLUTION = 600  # Seconds; "last used" is saved at most this often per key


@dataclass(frozen=True)
class Principal:
    """Who a request comes from: the key it carries."""

    id: str
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return _RANK[self.role] >= _RANK[ADMIN]

    def outranks(self, role: str) -> bool:
        return _RANK[self.role] > _RANK[role]


ROOT_PRINCIPAL = Principal(ROOT_ID, "Root", ROOT)


def _hash(token: str) -> str:
    # A plain hash suffices: the keys are random 256-bit tokens, nothing to brute-force
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class KeyStore:
    """The keys besides root, persisted as JSON in the config directory."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.CONFIG_DIR / "api_keys.json"
        self._lock = threading.Lock()
        self._keys: dict[str, dict] = {}  # By id
        self._by_hash: dict[str, str] = {}  # Key hash -> id
        try:
            for entry in json.loads(self.path.read_text(encoding="utf-8")):
                self._keys[entry["id"]] = entry
                self._by_hash[entry["hash"]] = entry["id"]
        except FileNotFoundError:
            pass

    def list(self) -> list[dict]:
        with self._lock:
            return [_public(e) for e in self._keys.values()]

    def get(self, key_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._keys.get(key_id)
            return _public(entry) if entry else None

    def create(self, name: str, role: str) -> tuple[dict, str]:
        """A new key. Returns its description and the key itself, which is not kept."""
        if role not in (ADMIN, CLIENT):
            raise ValueError(f"A key can be created as Admin or Client, not '{role}'.")
        token = TOKEN_PREFIX + secrets_module.token_urlsafe(32)
        entry = {"id": uuid.uuid4().hex[:12], "name": name, "role": role,
                 "hash": _hash(token), "hint": token[:len(TOKEN_PREFIX) + 4],
                 "created_at": time.time(), "last_used_at": None}
        with self._lock:
            self._check_name(name)
            self._keys[entry["id"]] = entry
            self._by_hash[entry["hash"]] = entry["id"]
            self._save()
        logger.info(f"🔑 Created the {role} key '{name}'.")
        return _public(entry), token

    def rename(self, key_id: str, name: str) -> dict:
        with self._lock:
            entry = self._keys[key_id]
            if name.lower() != entry["name"].lower():
                self._check_name(name)
            entry["name"] = name
            self._save()
            return _public(entry)

    def revoke(self, key_id: str) -> None:
        with self._lock:
            entry = self._keys.pop(key_id)
            self._by_hash.pop(entry["hash"], None)
            self._save()
        logger.warning(f"🔑 Revoked the {entry['role']} key '{entry['name']}'. Clients "
                       f"using it are now rejected.")

    def authenticate(self, token: str) -> Optional[Principal]:
        with self._lock:
            entry = self._keys.get(self._by_hash.get(_hash(token), ""))
            if entry is None:
                return None
            now = time.time()
            if not entry["last_used_at"] or now - entry["last_used_at"] > LAST_USED_RESOLUTION:
                entry["last_used_at"] = now
                self._save()
            return Principal(entry["id"], entry["name"], entry["role"])

    def _check_name(self, name: str) -> None:
        if any(e["name"].lower() == name.lower() for e in self._keys.values()):
            raise ValueError(f"There is a key named '{name}' already.")

    def _save(self) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(list(self._keys.values()), indent=1), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, self.path)


def _public(entry: dict) -> dict:
    """A key's description, without its hash."""
    return {k: v for k, v in entry.items() if k != "hash"}


_store: Optional[KeyStore] = None


def key_store() -> KeyStore:
    global _store
    if _store is None:
        _store = KeyStore()
    return _store


def authenticate(provided: Optional[str]) -> Optional[Principal]:
    """The principal the token belongs to, or None if it is no valid key."""
    if not provided:
        return None
    # Constant-time comparison: the token is a bearer credential, so a timing oracle
    # on it is worth avoiding even though the exposure is small. The other keys are
    # looked up by hash, which reveals nothing about them either.
    if secrets_module.compare_digest(provided, api_key()):
        return ROOT_PRINCIPAL
    return key_store().authenticate(provided)


async def require_api_key(authorization: Optional[str] = Header(default=None),
                          token: Optional[str] = Query(default=None)) -> Principal:
    """FastAPI dependency guarding every /v1 route: any valid key."""
    principal = authenticate(_token_from(authorization, token))
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or missing API key.",
                            headers={"WWW-Authenticate": "Bearer"})
    return principal


async def require_admin(principal: Principal = Depends(require_api_key)) -> Principal:
    """FastAPI dependency for what configures the server or reveals its credentials."""
    if not principal.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This needs an Admin or Root API key.")
    return principal


async def require_root(principal: Principal = Depends(require_api_key)) -> Principal:
    if principal.role != ROOT:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This needs the Root API key.")
    return principal


def check_token(provided: Optional[str]) -> bool:
    """Same check, for places that are not FastAPI dependencies (the VNC WebSocket)."""
    return authenticate(provided) is not None
