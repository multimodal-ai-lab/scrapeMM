"""The server's secret store.

Secrets are submitted through the web UI and encrypted at rest with a symmetric key
the server generates for itself on first start. Nobody is ever prompted for a password
and no admin has to manage a key.

What that buys, honestly: the ciphertext is useless in a volume snapshot, a backup, a
log or an accidentally committed file. What it does not buy: protection from anyone who
can read the config directory, because the server has to be able to decrypt unattended
and therefore so can they. Deployments that want the key out of the volume set
SCRAPEMM_MASTER_KEY (an env var or a Docker secret), which takes precedence over the
key file.

Values leave this module only towards the integrations. The API reports which secrets
are set, never what they are.
"""

import logging
import os
from typing import Optional

import yaml
from cryptography.fernet import Fernet, InvalidToken

from scrapemm.common.paths import APP_NAME
from .paths import MASTER_KEY_PATH, SECRETS_PATH

logger = logging.getLogger(APP_NAME)

SECRETS = {
    "x_bearer_token": "Bearer token of X (Twitter)",
    "telegram_api_id": "Telegram API ID",
    "telegram_api_hash": "Telegram API hash",
    "telegram_bot_token": "Telegram bot token",
    "bluesky_username": "Bluesky username",
    "bluesky_password": "Bluesky password",
    "tiktok_client_key": "TikTok client key",
    "tiktok_client_secret": "TikTok client secret",
    "reddit_client_id": "Reddit app client ID",
    "reddit_client_secret": "Reddit app client secret",
    "decodo_token": "Decodo Web Scraping API basic authentication token",
    "facebook_cookie": "Facebook cookie string",
    "instagram_cookie": "Instagram cookie string (needed for age-restricted content)",
    "archive_today_cookie": "Archive.today session cookies (established for you when you "
                            "solve the CAPTCHA in the web UI)",
}

# Secrets that are (potentially long and) multi-line, so the UI offers a textarea
MULTILINE_SECRETS = ("facebook_cookie", "instagram_cookie", "archive_today_cookie")

# Secrets the UI does not offer: they are not typed in but written by the server itself
MANAGED_SECRETS = ("archive_today_cookie",)

_cache: Optional[dict] = None


def _load_key() -> bytes:
    """Returns the key that encrypts the store, generating it on first use.

    A key handed in through the environment wins, so that a deployment can keep it out
    of the volume entirely without anything else changing.
    """
    if env_key := os.getenv("SCRAPEMM_MASTER_KEY"):
        return env_key.strip().encode()

    if MASTER_KEY_PATH.exists():
        return MASTER_KEY_PATH.read_bytes().strip()

    key = Fernet.generate_key()
    MASTER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    MASTER_KEY_PATH.write_bytes(key)
    _restrict_permissions(MASTER_KEY_PATH)
    logger.info(f"🔑 Generated a new secrets key at {MASTER_KEY_PATH}.")
    return key


def _restrict_permissions(path) -> None:
    """Makes the key readable by its owner only. Best-effort: on Windows the mode bits
    are not enforced, and a failure here must not stop the server from starting."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug(f"Could not restrict the permissions of {path}.", exc_info=True)


def _fernet() -> Fernet:
    return Fernet(_load_key())


def _load_secrets() -> dict:
    global _cache
    if _cache is not None:
        return _cache

    if not SECRETS_PATH.exists():
        _cache = {}
        return _cache

    try:
        _cache = yaml.safe_load(_fernet().decrypt(SECRETS_PATH.read_bytes())) or {}
    except (InvalidToken, ValueError):
        # The key no longer matches the store: someone replaced the key, or restored a
        # volume without it. Refusing loudly beats starting up with every integration
        # mysteriously unconfigured.
        raise RuntimeError(
            f"The secrets store at {SECRETS_PATH} cannot be decrypted with the current "
            f"key. Restore the matching key (or SCRAPEMM_MASTER_KEY), or delete the "
            f"store and re-enter the secrets in the web UI."
        )
    return _cache


def _save_secrets(data: dict) -> None:
    global _cache
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_bytes(_fernet().encrypt(yaml.safe_dump(data).encode()))
    _restrict_permissions(SECRETS_PATH)
    _cache = data


def get_secret(name: str) -> Optional[str]:
    return _load_secrets().get(name)


def set_secret(name: str, value: str) -> None:
    data = dict(_load_secrets())
    data[name] = value
    _save_secrets(data)


def remove_secret(name: str) -> bool:
    """Forgets a secret. Returns whether there was one to forget."""
    data = dict(_load_secrets())
    if name not in data:
        return False
    data.pop(name)
    _save_secrets(data)
    logger.info(f"Removed secret {name}.")
    return True


def is_set(name: str) -> bool:
    return bool(_load_secrets().get(name))


def describe_secrets() -> list[dict]:
    """What the API serves: which secrets exist and which of them are set. Never
    a value."""
    return [
        {
            "name": name,
            "description": description,
            "multiline": name in MULTILINE_SECRETS,
            "managed": name in MANAGED_SECRETS,
            "is_set": is_set(name),
        }
        for name, description in SECRETS.items()
    ]


def rotate_key() -> None:
    """Re-encrypts the store under a freshly generated key."""
    global _cache
    data = dict(_load_secrets())
    if os.getenv("SCRAPEMM_MASTER_KEY"):
        raise RuntimeError("The key comes from SCRAPEMM_MASTER_KEY; rotate it there.")
    MASTER_KEY_PATH.unlink(missing_ok=True)
    _cache = None
    _save_secrets(data)
    logger.info("🔑 Rotated the secrets key.")


def log_summary() -> None:
    logger.info("scrapeMM secrets configuration:")
    for name, description in SECRETS.items():
        logger.info(f" - {description}: {'✅ set' if is_set(name) else '⚠️ not set'}")
