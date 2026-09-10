# scrapeMM: Multimodal Web Retrieval
Simple web scraper to asynchronously retrieve webpages and access social media contents, fetching text along with media, i.e., images and videos.

This library aims to help developers and researchers to easily access multimodal data from the web and use it for LLM processing.

## Setup
* **If you want to download videos**: Then, the installation of [ffmpeg](https://ffmpeg.org/) is highly recommended.
In Conda, you can install it with `conda install -c conda-forge ffmpeg`.
* **If you want to scrape Perma.cc archive records or Facebook photos**, you'll need to install playwright with `pip install playwright` and running `playwright install` (add `--force` if an already installed version needs an update).

## Configure
To set the API secrets, run
```python
from scrapemm import configure_secrets
configure_secrets()
```

To set the Firecrawl URL, run
```python
from scrapemm import update_config
update_config(firecrawl_url="your_url")
```

## Usage

```python
from scrapemm import retrieve
import asyncio

if __name__ == "__main__":
    url = "https://www.snopes.com/fact-check/gauze-originate-from-gaza/"
    result = asyncio.run(retrieve(url))
    if result.success:
        print(result.get())
    else:
        print(result.errors)
```

`retrieve()` returns a `ScrapingResponse`. Its `result.get()` returns the content in the requested
format; `result.content` gives access to every format that was produced along the way:

| Attribute | Type | Content |
|---|---|---|
| `result.content.html` | `str` | The raw HTML code of the page |
| `result.content.markdown` | `str` | The page text in Markdown, media referenced by hyperlink |
| `result.content.multimodal` | `MultimodalSequence` | The page text in Markdown with all media downloaded and embedded |

Use `output_format` to tell scrapeMM which format you need (default: `"multimodal"`):

```python
result = asyncio.run(retrieve(url, output_format="html"))
print(result.content.html)
```

The formats are produced one after another — HTML, then Markdown, then the `MultimodalSequence` —
and scraping stops as soon as the requested format is reached. So the earlier formats come along
for free (whenever the used retrieval method had access to them), while nothing beyond the
requested format is computed and media gets downloaded only for `"multimodal"`. `result.success`
tells you whether the requested format could be produced.
`scrapeMM` will ask you for the **API secrets** needed for the integrations. You may skip them if you don't need them.

You will also be prompted to choose a **password** that is used to secure the secrets in an encrypted file.

## Caching
Successful retrievals are cached in memory for 24 hours, so scraping the same URL again is
instantaneous. The cache is *not* persisted, i.e., it is empty again after the process ended.
`result.from_cache` tells you whether a response came from the cache.

To change the caching duration (in seconds) for the current process, run
```python
from scrapemm import set_cache_ttl
set_cache_ttl(60 * 60)  # cache for one hour
set_cache_ttl(0)  # disable caching
```
Use `update_config(cache_ttl=3600)` instead to persist the duration across processes, and
`clear_cache()` to empty the cache. To bypass the cache for a single call, pass
`retrieve(url, use_cache=False)`.

## CAPTCHAs and Blacklisted Domains
Every scraped page is checked for CAPTCHA challenges (Cloudflare, reCAPTCHA, hCaptcha, DataDome,
AWS WAF, PerimeterX, and others). If a challenge was served instead of the page content, the
response carries a `CaptchaEncounteredError` and the URL's domain is put on a blacklist that is
persisted to `blacklist.yaml` in scrapeMM's config directory. Retrieving any URL of a blacklisted
domain then fails right away with an `UnsupportedDomainError` telling why the domain was
blacklisted.

```python
from scrapemm import get_blacklisted_domains, unblacklist_domain, blacklist_domain

get_blacklisted_domains()  # -> {"example.com": "Method decodo encountered a Cloudflare challenge. ..."}
unblacklist_domain("example.com")  # Retrieve that domain again
blacklist_domain("example.com", "Paywalled")  # Exclude a domain manually
```

A domain gets blacklisted only if *all* retrieval methods failed, so a CAPTCHA on one method does
not exclude a domain that another method can still scrape. Blacklisting applies to the registrable
domain, i.e., including all of its subdomains.

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
- ✅ Threads: posts only (profiles TBD)
- ✅ Reddit: posts only (needs a Reddit app, see https://www.reddit.com/prefs/apps)

### Archiving Services
- ✅ Perma.cc
- ✅ Archive.today: Rarely ending up in TimeoutErrors
- ✅ MediaVault (mvau.lt)
- ✅ Internet Archive (web.archive.org)
- ✅ AwesomeScreenshot.com
- ✅ Ghostarchive (ghostarchive.org)
