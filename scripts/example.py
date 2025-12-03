from scrapemm import retrieve
import asyncio

if __name__ == "__main__":
    url = "https://www.instagram.com/reel/ClqTRryA6np/?utm_source=ig_embed&ig_rid=1ea336bf-737d-4526-8e12-233bf49f0488"
    result = asyncio.run(retrieve(url))
    print(result)
