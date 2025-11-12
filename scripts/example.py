from scrapemm import retrieve
import asyncio

if __name__ == "__main__":
    url = "https://verafiles.org"
    result = asyncio.run(retrieve(url))
    print(result)
