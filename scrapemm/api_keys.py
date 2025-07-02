from getpass import getpass
from typing import Optional

import keyring

from config import global_vars, save_globals

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
    print("Starting API key configuration.")

    for key_name, description in API_KEYS.items():
        key_value = get_api_key(key_name)
        if all_keys or not key_value:
            # Get and save the missing API key
            user_input = getpass(f"Please enter the {description} (leave empty to skip): ")
            if user_input:
                keyring.set_password(KEYRING_SERVICE_NAME, key_name, user_input)

    global_vars["api_keys_configured"] = True
    save_globals()

    print("API keys configured successfully! If you want to change them, go to "
          "config/api_keys.yaml and set 'api_keys_configured' to 'false'.")


def get_api_key(key_name: str) -> Optional[str]:
    """Retrieves the API key from the system's credential store."""
    return keyring.get_password(KEYRING_SERVICE_NAME, key_name)


if not global_vars.get("api_keys_configured"):
    configure_api_keys(all_keys=True)
