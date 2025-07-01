# Multimodal Web Retrieval
Utilities to asynchronously scrape webpages and access social media, supporting text, images, and videos.

This library aims to help developers and researchers to easily access multimodal data from the web.

## How it works
```
Input:                                  Output:
URL (string)   -->   retrieve()   -->   MultimodalSequence
```
The `MultimodalSequence` is a sequence of Markdown-formatted text and media provided by the [ezMM](https://github.com/multimodal-ai-lab/ezmm) library.

Web scraping is done with [Firecrawl](https://github.com/mendableai/firecrawl).

## Supported Proprietary APIs
- ✅ X/Twitter
- ✅ Telegram
- ⏳ Facebook
- ⏳ Instagram
- ⏳ Threads
