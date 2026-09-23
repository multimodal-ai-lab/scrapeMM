"""The scrapeMM server: everything that actually scrapes.

This subpackage is **not** installable from PyPI. The published `scrapeMM` package is
the client alone; the server is distributed as a Docker image built from this repo,
because a pip install cannot provide what the server needs anyway (Playwright's
browsers, FFmpeg, and the Xvfb display the Archive.today CAPTCHA panel drives).

Its dependencies are declared in `requirements-server.txt`. Importing this package
without them raises the error below rather than a bare ModuleNotFoundError from
somewhere three levels down.
"""

try:
    import playwright  # noqa: F401  (a representative server-only dependency)
except ModuleNotFoundError as e:  # pragma: no cover - depends on the environment
    raise ModuleNotFoundError(
        "The scrapeMM server's dependencies are not installed. The server is meant to "
        "run from its Docker image (`docker compose up -d`). To work on it from a "
        "checkout instead, run `pip install -r requirements-server.txt` and "
        "`playwright install`."
    ) from e
