from scrapemm import retrieve
import asyncio

if __name__ == "__main__":
    url = "https://edition.cnn.com/2025/07/02/europe/north-korea-troops-russia-ukraine-intl-cmd"
    result = asyncio.run(retrieve(url))
    print(result)
    if result:
        result.render()
