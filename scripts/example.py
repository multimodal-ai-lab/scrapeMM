from scrapemm import retrieve
import asyncio

if __name__ == "__main__":
    url = "https://www.tiktok.com/@xxxx.xxxx5743/video/7521704371109793046"
    loop = asyncio.get_event_loop()
    result = loop.run_until_complete(retrieve(url))
    print(result)
