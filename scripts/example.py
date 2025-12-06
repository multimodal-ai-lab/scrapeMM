from scrapemm import retrieve
import asyncio

if __name__ == "__main__":
    url = "https://www.tiktok.com/@realdonaldtrump/video/7433870905635409198"
    result = asyncio.run(retrieve(url))
    if result.errors:
        print(result.errors)
    else:
        print(result.content)
        print(str(result.content))
