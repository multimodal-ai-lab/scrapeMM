# scrapeMM: Multimodal Web Retrieval
Simple web scraper to asynchronously retrieve webpages and access social media contents, fetching text along with media, i.e., images and videos.

This library aims to help developers and researchers to easily access multimodal data from the web and use it for LLM processing.

## Setup
* **If you want to download videos**: Then, the installation of [ffmpeg](https://ffmpeg.org/) is highly recommended.
In Conda, you can install it with `conda install -c conda-forge ffmpeg`. Platforms like YouTube and
Facebook serve video and audio as separate streams, and merging them needs ffmpeg. Without it,
videos are downloaded **without sound**. scrapeMM also normalizes downloaded videos into a format
browsers can play; that step additionally needs `ffprobe`, which ships with a full ffmpeg install
(the `imageio-ffmpeg` package provides `ffmpeg` only).
* **Install Playwright dependencies** (used by multiple integrations) running `playwright install` (add `--force` if an already installed version needs an update).

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

A single Firecrawl instance is the throughput ceiling of the whole pipeline, so you can point
scrapeMM at **several** of them. It probes all of them at startup, spreads its scrapes across
those that respond, and retries a busy instance on another one instead of waiting:
```python
update_config(firecrawl_urls=["http://host-a:3002", "http://host-b:3002"])
```

### Platform Cookies
Some content is only served to a logged-in session. Set the respective cookie via
`configure_secrets()` (or `override_secret("<name>")`) to reach it:

| Secret | Unlocks |
|---|---|
| `facebook_cookie` | Facebook posts that require login |
| `instagram_cookie` | Age-restricted Instagram posts ("can't be seen by certain audiences") |

Facebook hides posts that fact-checkers flagged as false information behind an interstitial
that yt-dlp cannot read. scrapeMM falls back to reading the video straight out of the page
source in that case, so flagged posts stay retrievable.

### Archive.today Access
Archive.today guards its snapshot pages with a Google reCAPTCHA, and a solved one unlocks
them for only about **five minutes**. scrapeMM does **not** solve CAPTCHAs, so instead of
waiting for you at the moment of each request it **collects work for your next solve**:

1. A snapshot that was retrieved before is served from a **permanent cache** — a capture
   never changes, and only its page is gated, so retrieving it once settles it for good.
2. Otherwise scrapeMM fetches the page with the stored session. Media comes from ungated
   subdomains, so images and videos download with no session at all.
3. If the page is gated, the request **fails immediately** with a `CaptchaEncounteredError`
   and the URL goes into a **buffer**. Nothing blocks.
4. When you solve the CAPTCHA, everything buffered is retrieved and cached within those
   five minutes:
   ```bash
   python scripts/configure_archive_today.py
   ```
   (add a number of seconds to wait, e.g. `900`, when running headless — it prints an SSH
   command to reach the browser window)

So the working rhythm is: let a batch run and collect misses, solve one CAPTCHA, then run
the batch again — the second time it is served from the cache. All mirrors (`archive.is`,
`archive.ph`, …) share one session and one cache.

```python
from scrapemm import (get_archive_today_buffer, clear_archive_today_buffer,
                      retrieve_buffered_archive_today, count_cached_archive_today_pages)

get_archive_today_buffer()          # URLs waiting for the next solve
count_cached_archive_today_pages()  # how many pages are cached
retrieve_buffered_archive_today()   # resume a drain the gate interrupted, session permitting
clear_archive_today_buffer()        # forget the backlog
```
The buffer lives in `archive_today_buffer.json` and the pages in `archive_today_pages/`,
both in scrapeMM's config directory.

Two opt-ins sit beside this. `update_config(archive_today_interactive_solve=True)` restores
the old behaviour of opening the browser and waiting for you at the moment of a gated
request — reasonable for a one-off, attended retrieval, not for batches. And
`update_config(archive_today_screenshot_fallback=True)` serves the snapshot's screenshot
and metadata (not its text) when there is no session, so an unattended run gets *something*
rather than an error.

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

Automatic blacklistings **expire after 7 days**: CAPTCHA gates are often transient (a burst of
parallel requests can trigger one on a domain that is perfectly retrievable an hour later), so
excluding a domain forever would quietly erode coverage over time. Change the duration with
```python
update_config(blacklist_ttl=24 * 60 * 60)  # Retry blacklisted domains after a day
update_config(blacklist_ttl=0)  # Never expire
```
Domains you blacklist yourself via `blacklist_domain()` are **permanent** — they express a
decision, not an observation — and stay excluded until you call `unblacklist_domain()`.

Domains that are served by an integration (`perma.cc`, `archive.today`, `x.com`, ...) are never
blacklisted automatically: their CAPTCHA gates are transient, so blacklisting would disable the
respective integration for good.

## Speeding Up Retrieval
By default, scrapeMM tries its retrieval methods strictly one after another, so a slow method
delays every method behind it by its full timeout budget. **Hedging** gives each method only a
head start instead: once the delay elapses, the next method is launched *alongside* it, the first
success wins, and the rest are cancelled. A method that fails early hands over immediately,
without waiting out the delay.

```python
result = asyncio.run(retrieve(url, hedging_delay=5))  # 5 s head start per method
```
Use `update_config(hedging_delay=5)` to enable it process-wide. Hedging is **disabled by default**
because it duplicates work — and, for paid methods such as Decodo, duplicates billable requests.

## How it works
```
Input:                                  Output:
URL (string)   -->   retrieve()   -->   MultimodalSequence
```
The `MultimodalSequence` is a sequence of Markdown-formatted text and media provided by the [ezMM](https://github.com/multimodal-ai-lab/ezmm) library.

Web scraping is done with [Firecrawl](https://github.com/mendableai/firecrawl) and [Decodo](https://decodo.com/).

Media is collected from `<img>` and `<video>` tags, from CSS background images, and from
embedded players of the common video platforms (an `<iframe>` pointing at YouTube, Vimeo,
Dailymotion, …), which are downloaded with yt-dlp. `max_video_size` caps every one of those
downloads just like it caps the ones of the platform integrations; oversized videos are skipped
without downloading a byte whenever the server announces a `Content-Length`.

Images are taken at their highest available resolution: `srcset` candidates are compared, and
lazy-loading attributes (`data-src`, `data-lazy-src`, `data-original`, …) are honoured, so pages
that ship a placeholder in `src` still yield their real media. Media references are resolved
against the page URL, no matter whether they are absolute, protocol-relative (`//cdn/x.jpg`),
root-relative (`/x.jpg`) or document-relative (`img/x.jpg`).

## Supported Platforms
### Social Media
- ✅ X/Twitter
- ✅ Telegram
- ✅ Bluesky
- ✅ TikTok
- ✅ YouTube
- ✅️ Instagram: works for most content
- ✅️ Facebook
- ✅ Threads: posts only (profiles TBD)
- ✅ Reddit: posts only

### Archiving Services
- ✅ Perma.cc
- ✅ Archive.today: Rarely ending up in TimeoutErrors
- ✅ MediaVault (mvau.lt)
- ✅ Internet Archive (web.archive.org)
- ✅ AwesomeScreenshot.com
- ✅ Ghostarchive (ghostarchive.org)
