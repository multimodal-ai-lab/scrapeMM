# scrapeMM: Multimodal Web Retrieval
Simple web scraper to asynchronously retrieve webpages and access social media contents, fetching text along with media, i.e., images and videos.

This library aims to help developers and researchers to easily access multimodal data from the web and use it for LLM processing.

## Setup
* **If you want to download videos**: Then, the installation of [ffmpeg](https://ffmpeg.org/) is highly recommended.
In Conda, you can install it with `conda install -c conda-forge ffmpeg`.
* **If you want to scrape Perma.cc archive records or Facebook photos**, you'll need to install playwright with `pip install playwright` and running `playwright install`.

## Usage
```python
from scrapemm import retrieve
import asyncio

if __name__ == "__main__":
    url = "https://www.snopes.com/fact-check/gauze-originate-from-gaza/"
    result = asyncio.run(retrieve(url))
    if result.errors:
        print(result.errors)
    else:
        print(result.content)
```
`scrapeMM` will ask you for the **API secrets** needed for the integrations. You may skip them if you don't need them.

You will also be prompted to choose a **password** that is used to secure the secrets in an encrypted file.

## How it works
```
Input:                                  Output:
URL (string)   -->   retrieve()   -->   MultimodalSequence
```
The `MultimodalSequence` is a sequence of Markdown-formatted text and media provided by the [ezMM](https://github.com/multimodal-ai-lab/ezmm) library.

Web scraping is done with [Firecrawl](https://github.com/mendableai/firecrawl) and [Decodo](https://decodo.com/).

## Supported Platforms
### Social Media
- ✅ X/Twitter
- ✅ Telegram
- ✅ Bluesky
- ✅ TikTok
- ✅ YouTube
- (✅️) Instagram: works for most content
- ✅️ Facebook
- ❌ Threads: TBD
- ❌ Reddit: TBD

### Archiving Services
- ✅ Perma.cc
- (✅) Archive.today: Sometimes ending up in TimeoutErrors, generally pretty slow
- ✅ MediaVault (mvau.lt)
- ❌ Wayback Machine, Internet Archive (web.archive.org)
- ❌ AwesomeScreenshot.com
- ❌ Ghost Archive (ghostarchive.org)
