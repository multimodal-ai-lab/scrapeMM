"""Pass Archive.today's access check once, in scrapeMM's own browser, and store the
resulting session so that subsequent retrievals can use it.

Archive.today gates its snapshots with a CAPTCHA, served with an HTTP 429 status.
scrapeMM does not solve captchas: running this script opens a browser window in which
*you* pass the check — once per mirror domain, since every mirror is a separate
Cloudflare zone with its own clearance cookie. Cloudflare binds that cookie to the
browser and IP address that obtained it, which is why the checks have to be passed in
this window rather than in your everyday browser or on another machine.
"""
import asyncio

from scrapemm import configure_archive_today_session

if __name__ == "__main__":
    succeeded = asyncio.run(configure_archive_today_session())
    print("Session stored." if succeeded else "No session stored.")
