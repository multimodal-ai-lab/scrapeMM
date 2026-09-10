import scrapemm  # import is sufficient to trigger secret configuration, if not configured yet
from scrapemm.secrets import configure_secrets, override_secret, remove_secret

configure_secrets(all_keys=False)
# override_secret("key_name")
# remove_secret("youtube_cookie")
