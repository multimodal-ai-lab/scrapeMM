from getpass import getpass
from typing import Optional

import keyring

from scrapemm.common import get_config_var, update_config, APP_NAME, CONFIG_PATH

API_KEYS = {
    "x_bearer_token": "Bearer token of X (Twitter)",
    "telegram_api_id": "Telegram API ID",
    "telegram_api_hash": "Telegram API hash",
    "telegram_bot_token": "Telegram bot token",
    "bluesky_username": "Bluesky username",
    "bluesky_password": "Bluesky password",
}
KEYRING_SERVICE_NAME = APP_NAME


def configure_api_keys(all_keys: bool = False):
    """Gets the API keys from the user by running a CLI dialogue.
    Saves them via keyring in the system's credential store."""
    prompted = False
    for key_name, description in API_KEYS.items():
        key_value = get_api_key(key_name)
        if all_keys or not key_value:
            # Get and save the missing API key
            user_input = getpass(f"Please enter the {description} (leave empty to skip): ")
            prompted = True
            if user_input:
                keyring.set_password(KEYRING_SERVICE_NAME, key_name, user_input)

    update_config(api_keys_configured=True)

    if prompted:
        print("API keys configured successfully! If you want to change them, go to "
              f"{CONFIG_PATH.as_posix()} and set 'api_keys_configured' to 'false' or "
              f"run scrapemm.api_keys.configure_api_keys().")


def get_api_key(key_name: str) -> Optional[str]:
    """Retrieves the API key from the system's credential store."""
    return keyring.get_password(KEYRING_SERVICE_NAME, key_name)


if not get_config_var("api_keys_configured"):
    configure_api_keys()
