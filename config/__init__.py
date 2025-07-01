import yaml

globals = yaml.safe_load(open("config/globals.yaml"))

x_bearer_token = globals.get("x_bearer")

telegram_api_id = globals.get("telegram_api_id")
telegram_api_hash = globals.get("telegram_api_hash")
telegram_bot_token = globals.get("telegram_bot_token")

firecrawl_url = globals.get("firecrawl_url")

