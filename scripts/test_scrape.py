#!/usr/bin/env python3
"""
Simple test script to scrape a URL and display the results.
Usage: python scripts/test_scrape.py <url>
"""

import asyncio
import sys
from scrapemm import retrieve


async def test_scrape(url: str):
    """Scrape a URL and display the results."""
    print(f"🔍 Scraping: {url}\n")

    result = await retrieve(url)

    if result:
        print("✅ Successfully scraped!\n")
        print("=" * 80)
        print("CONTENT:")
        print("=" * 80)
        print(result)
        print("=" * 80)
        print(f"\nContent length: {len(str(result))} characters")
    else:
        print("❌ Failed to scrape the URL.")
        return 1

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_scrape.py <url>")
        print("\nExample:")
        print("  python scripts/test_scrape.py https://example.com")
        sys.exit(1)

    url = sys.argv[1]
    exit_code = asyncio.run(test_scrape(url))
    sys.exit(exit_code)
