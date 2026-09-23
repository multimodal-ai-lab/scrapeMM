"""Retrieve a few URLs through a running scrapeMM server.

Start one first (`docker compose up -d`), then point this at it -- either here or via
the SCRAPEMM_API_URL / SCRAPEMM_API_KEY environment variables.
"""
import asyncio

import scrapemm

scrapemm.configure(api_url="http://localhost:8080", api_key="_vh3p5HotXT-YE2XDcICD_UZLehwJh-6mYHHr5iVbzY")

if __name__ == "__main__":
    url = "https://www.zeit.de/politik/deutschland/2025-07/spionage-iran-festnahme-anschlag-juden-berlin-daenemark"
    result = asyncio.run(scrapemm.retrieve(url))
    print(result.get() if result.success else result.errors)
