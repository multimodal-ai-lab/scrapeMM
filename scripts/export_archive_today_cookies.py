"""Writes the Archive.today session stored in the secrets file into the cookie file that
scrapeMM ships as its default (`scrapemm/integrations/archive_today_cookies.txt`).

Usage: export_archive_today_cookies.py [OUTPUT_PATH] [--no-clearance]

Use this after `configure_archive_today.py` to turn the session you just established into
the new default. Note that a `cf_clearance` cookie is bound to the browser and IP address
that obtained it, so it is of no use on another machine — only the remaining cookies are.
Keep in mind that the file becomes part of the package: do not put a session in there that
you are not willing to share.
"""
import sys
from pathlib import Path

from scrapemm.integrations.archive_today import DEFAULT_COOKIES_PATH
from scrapemm.secrets import get_secret
from scrapemm.util import parse_cookies, to_netscape_cookies

HEADER = ("Default Archive.today session cookies shipped with scrapeMM. They help to get\n"
          "past Archive.today's access check. Run scrapemm.configure_archive_today_session()\n"
          "to establish your own session, which takes precedence over this file.")

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    drop_clearance = "--no-clearance" in sys.argv
    path = Path(args[0]) if args else DEFAULT_COOKIES_PATH

    cookies = parse_cookies(get_secret("archive_today_cookie") or "")
    if not cookies:
        sys.exit("No Archive.today session stored. Run scripts/configure_archive_today.py first.")

    if drop_clearance:
        # cf_clearance is bound to one browser and IP address, so it is dead weight in a
        # file that gets shared with others
        cookies = [c for c in cookies if c["name"] != "cf_clearance"]

    path.write_text(to_netscape_cookies(cookies, header=HEADER), encoding="utf-8", newline="\n")

    domains = sorted({c["domain"].lstrip(".") for c in cookies})
    clearances = sorted({c["domain"].lstrip(".") for c in cookies if c["name"] == "cf_clearance"})
    print(f"Wrote {len(cookies)} cookies for {len(domains)} mirrors to {path}")
    print(f"  mirrors            : {', '.join(domains)}")
    print(f"  with cf_clearance  : {', '.join(clearances) or 'none'}")
    print("\nThe stored session still takes precedence over the file. To verify that the file\n"
          "works on its own, drop the secret:\n"
          "  python -c \"from scrapemm.secrets import remove_secret; "
          "remove_secret('archive_today_cookie')\"")
