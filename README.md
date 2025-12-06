# scrapeMM: Multimodal Web Retrieval
Simple web scraper to asynchronously retrieve webpages and access social media contents, fetching text along with media, i.e., images and videos.

This library aims to help developers and researchers to easily access multimodal data from the web and use it for LLM processing.

## Usage
```python
from scrapemm import retrieve
import asyncio

url = "https://example.com"
loop = asyncio.get_event_loop()
result = loop.run_until_complete(retrieve(url))
result.render()
```
`scrapeMM` will ask you for the **API keys** needed for the social media integrations. You may skip them if you don't need them. 
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
- ⏳ Facebook: done for videos but not for images yet
- ❌ Threads: TBD
- ❌ Reddit: TBD

### Archiving Services
- ❌ Perma.cc
- ❌ Archive.today
- ❌ Wayback Machine, Internet Archive (web.archive.org)
- ❌ AwesomeScreenshot.com
- ⏳ MediaVault (mvau.lt): Works for images but not for videos yet
- ❌ Ghost Archive (ghostarchive.org)
