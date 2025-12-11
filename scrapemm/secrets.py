import base64
import logging
import sys
from getpass import getpass

import yaml
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from scrapemm.common import get_config_var, update_config, CONFIG_PATH, CONFIG_DIR
from scrapemm.util import get_multiline_user_input

logger = logging.getLogger("scrapeMM")

SECRETS = {
    "x_bearer_token": "Bearer token of X (Twitter)",
    "telegram_api_id": "Telegram API ID",
    "telegram_api_hash": "Telegram API hash",
    "telegram_bot_token": "Telegram bot token",
    "bluesky_username": "Bluesky username",
    "bluesky_password": "Bluesky password",
    "tiktok_client_key": "TikTok client key",
    "tiktok_client_secret": "TikTok client secret",
    "decodo_username": "Decodo Web Scraping API username",
    "decodo_password": "Decodo Web Scraping API password",
    "youtube_cookie": "YouTube cookie string",
    "facebook_cookie": "Facebook cookie string",
}

SALT = b'\xa4\x93\xf1\x88\x13\x88'
SECRETS_PATH = CONFIG_DIR / "secrets"

_password_cache = None


def _derive_key(password: str) -> bytes:
    """Derive a (symmetric) Fernet key from the password."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SALT,
        iterations=100_000,
        backend=default_backend()
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _encrypt_dict(data: dict, fernet: Fernet):
    raw = yaml.safe_dump(data).encode()
    return fernet.encrypt(raw)


def _decrypt_dict(token: bytes, fernet: Fernet):
    decrypted = fernet.decrypt(token)
    return yaml.safe_load(decrypted)


def _get_password(prompt="🔐 Enter password to unlock secrets: ", pwd: str | None = None) -> Fernet:
    """Prompts the user to enter a password and returns a Fernet object.
    Re-uses the password if it was already entered before."""
    global _password_cache
    password = pwd if pwd is not None else _password_cache
    if password is None:
        password = getpass(prompt, stream=sys.stdout)
    _password_cache = password
    return _load_fernet(password)


def _load_fernet(pwd: str) -> Fernet:
    key = _derive_key(pwd)
    return Fernet(key)


def _load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        return {}

    with open(SECRETS_PATH, "rb") as f:
        encrypted = f.read()

    # Try with empty password first
    fernet = _get_password(pwd="")
    try:
        return _decrypt_dict(encrypted, fernet)
    except (InvalidToken, ValueError):
        pass

    # Get the password from the user and decrypt the secrets
    while True:
        fernet = _get_password()
        try:
            return _decrypt_dict(encrypted, fernet)
        except (InvalidToken, ValueError):
            print("❌ Incorrect password or corrupted file.")
            global _password_cache
            _password_cache = None


def _save_secrets(data: dict):
    data = data.copy()

    fernet = _get_password("🔐 Enter password to encrypt secrets: ")
    encrypted = _encrypt_dict(data, fernet)

    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SECRETS_PATH, "wb") as f:
        f.write(encrypted)


def get_secret(name: str) -> str | None:
    data = _load_secrets()
    return data.get(name)


def set_secret(name: str, value: str):
    data = _load_secrets()
    data[name] = value
    _save_secrets(data)


def list_secrets():
    data = _load_secrets()
    return [k for k in data]


def enter_password(pwd: str):
    """Saves the given password into the cache."""
    _get_password(pwd=pwd)


def _set_new_password():
    # Delete existing secrets
    if SECRETS_PATH.exists():
        SECRETS_PATH.unlink()

    # Set up a new password
    _get_password("🔐 Enter a password to encrypt your secrets (you'll need it later to decrypt them): ")


def override_secret(key_name: str):
    """Prompts the user to enter a new value for the given secret key. Does nothing
    when nothing entered."""
    description = SECRETS[key_name]
    if key_name in ["youtube_cookie", "facebook_cookie"]:
        user_input = get_multiline_user_input(f"Please enter the {description}. Hit Ctrl-D or Ctrl-Z to save it.")
    else:
        user_input = getpass(f"Please enter the {description} (leave empty to skip): ", stream=sys.stdout)
    if user_input:
        set_secret(key_name, user_input)


def remove_secret(key_name: str):
    data = _load_secrets()
    data.pop(key_name, None)
    _save_secrets(data)


def configure_secrets(all_keys: bool = False):
    """Gets the secrets from the user by running a CLI dialogue.
    Saves them in an encrypted file. Only overrides an existing secret on non-empty input."""
    logger.info("Starting secrets configuration...")

    for key_name in SECRETS.keys():
        key_value = get_secret(key_name)
        if all_keys or not key_value:
            # Get and save the missing API key
            override_secret(key_name)

    update_config(api_keys_configured=True)

    logger.info("API keys configured successfully! If you want to change them, go to "
                f"{CONFIG_PATH.as_posix()} and set 'api_keys_configured' to 'false' or "
                f"run scrapemm.api_keys.configure_api_keys().")


# Ensure secrets file exists
if not SECRETS_PATH.exists():
    _set_new_password()

# Ensure secrets are configured
if not get_config_var("api_keys_configured"):
    configure_secrets()

# Print secret summary
logger.info("ScrapeMM secrets configuration:")
for key_name, description in SECRETS.items():
    logger.info(f" - {description}: {'✅ set' if get_secret(key_name) else '⚠️ not set'}")
