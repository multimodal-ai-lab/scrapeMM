from scrapemm import retrieve
import asyncio

if __name__ == "__main__":
    urls = [
        "https://perma.cc/L7DR-W3K6",
        "https://perma.cc/U7BY-FWN4",
        "https://perma.cc/MC46-CF8H",
        "https://perma.cc/3LSM-MUQT",
        "https://perma.cc/PZM5-3T6Y?type=image",
        "https://perma.cc/V56T-T4UR",
        "https://perma.cc/LC9M-D4TQ",
        "https://perma.cc/ZA9X-PWQQ",
        "https://perma.cc/U3JC-79UN",
        "https://perma.cc/57C7-HT6N?type=image",
        "https://perma.cc/U66X-WFNS?type=image",
        "https://perma.cc/4VHZ-THNN",
        "https://perma.cc/9GT8-XRU7",
        "https://perma.cc/LKD5-NJPZ",
    ]
    results = asyncio.run(retrieve(urls))
    for result in results:
        print(result.get() if result.success else result.errors)
