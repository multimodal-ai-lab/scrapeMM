"""Pass Archive.today's access check once, in scrapeMM's own browser, and store the
resulting session so that subsequent retrievals can use it.

Archive.today gates its snapshot pages with a reCAPTCHA, served with an HTTP 429 status.
scrapeMM does not solve captchas: running this script opens a browser window in which
*you* pass the check. Once is enough — scrapeMM serves every mirror domain via
archive.ph, so a single session covers all of them. Archive.today binds the session to
the browser that obtained it, which is why the check has to be passed in this window
rather than in your everyday browser or on another machine.

You do not strictly need to run this ahead of time: by default scrapeMM opens the browser
and prompts you the moment a retrieval hits the access check. Running it up front just
gets the prompt out of the way. A session lasts about five minutes.

Usage: configure_archive_today.py [SECONDS_TO_WAIT]

On a headless machine the script prints how to reach the browser window from your local
one (an SSH tunnel to Chrome's debugging port, no extra software on the server).
"""
import asyncio
import sys

from scrapemm import configure_archive_today_session

if __name__ == "__main__":
    # Seconds to wait for you to pass the check. Raise it when the browser runs on a remote
    # machine and you first have to set up the tunnel that shows you its window.
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 300

    succeeded = asyncio.run(configure_archive_today_session(timeout=timeout))
    print("Session stored." if succeeded else "No session stored.")
