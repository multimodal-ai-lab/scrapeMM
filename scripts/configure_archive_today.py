"""Pass Archive.today's access check once, in scrapeMM's own browser, and store the
resulting session so that subsequent retrievals can use it.

Archive.today gates its snapshots with a CAPTCHA, served with an HTTP 429 status.
scrapeMM does not solve captchas: running this script opens a browser window in which
*you* pass the check — once per mirror domain, since every mirror is a separate
Cloudflare zone with its own clearance cookie. Cloudflare binds that cookie to the
browser and IP address that obtained it, which is why the checks have to be passed in
this window rather than in your everyday browser or on another machine.

Usage: configure_archive_today.py [SECONDS_TO_WAIT_PER_MIRROR]

On a headless machine the script prints how to reach the browser window from your local
one (an SSH tunnel to Chrome's debugging port, no extra software on the server).
"""
import asyncio
import sys

from scrapemm import configure_archive_today_session

if __name__ == "__main__":
    # Seconds to wait per mirror. Raise it when the browser runs on a remote machine and
    # you first have to set up the tunnel that shows you its window.
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 300

    succeeded = asyncio.run(configure_archive_today_session(timeout=timeout))
    print("Session stored." if succeeded else "No session stored.")
