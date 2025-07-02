from getpass import getpass
from typing import Optional

import keyring

from scrapemm.common import config, save_to_config

API_KEYS = {
    "x_bearer_token": "Bearer token of X (Twitter)",
    "telegram_api_id": "Telegram API ID",
    "telegram_api_hash": "Telegram API hash",
    "telegram_bot_token": "Telegram bot token",
}
KEYRING_SERVICE_NAME = "scrapemm"


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

    save_to_config(dict(api_keys_configured=True))

    if prompted:
        print("API keys configured successfully! If you want to change them, go to "
              "config/api_keys.yaml and set 'api_keys_configured' to 'false'.")


def get_api_key(key_name: str) -> Optional[str]:
    """Retrieves the API key from the system's credential store."""
    return keyring.get_password(KEYRING_SERVICE_NAME, key_name)


if not config.get("api_keys_configured"):
    configure_api_keys()
