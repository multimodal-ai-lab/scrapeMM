"""The published distribution must contain the client and nothing else.

This is the kind of property that rots silently: a stray `include`, a new subpackage, or
a setuptools change would start shipping the scraping stack to every `pip install
scrapeMM` without anything failing. Building the artefacts and looking inside is the
only way to know.

Skipped when the `build` package is unavailable, so a bare checkout still runs green.
"""

import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("build", reason="needs the `build` package to produce artefacts")

REPO = Path(__file__).resolve().parent.parent

# Directories that exist in the repository but have no business in a client install
EXCLUDED = ("scrapemm/server", "/testing/", "/scripts/", "/ui/", "/docker/")


@pytest.fixture(scope="module")
def artefacts(tmp_path_factory):
    """Builds a real wheel and sdist, the way a release would."""
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(out)],
        cwd=REPO, capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not build the distribution here:\n{result.stderr[-800:]}")

    wheel = next(out.glob("*.whl"))
    sdist = next(out.glob("*.tar.gz"))
    return {
        "wheel": [n.replace("\\", "/") for n in zipfile.ZipFile(wheel).namelist()],
        "sdist": [n.replace("\\", "/") for n in tarfile.open(sdist).getnames()],
    }


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_no_server_code_is_published(artefacts, kind):
    """`pip install scrapeMM` must not be able to put a scraping stack on a machine."""
    leaked = [n for n in artefacts[kind] if "scrapemm/server" in n]
    assert not leaked, (
        f"the {kind} ships server code: {leaked[:5]}. The server is distributed as a "
        f"Docker image built from this repo, not through PyPI.")


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_no_repository_scaffolding_is_published(artefacts, kind):
    leaked = [n for n in artefacts[kind]
              if any(part in n for part in EXCLUDED if part != "scrapemm/server")]
    assert not leaked, f"the {kind} ships files it should not: {leaked[:5]}"


def test_the_client_itself_is_published(artefacts):
    """The counterpart: excluding too much would be just as wrong."""
    wheel = artefacts["wheel"]
    for expected in ("scrapemm/__init__.py",
                     "scrapemm/client/client.py",
                     "scrapemm/common/wire.py"):
        assert expected in wheel, f"the wheel is missing {expected}"


def test_declared_dependencies_stay_client_sized():
    """A server dependency creeping into the client's list would undo the split even
    with the code excluded."""
    import tomllib

    with open(REPO / "pyproject.toml", "rb") as f:
        dependencies = tomllib.load(f)["project"]["dependencies"]

    names = {d.split("[")[0].split("~")[0].split(">")[0].split("=")[0].strip().lower()
             for d in dependencies}
    server_only = {"playwright", "seleniumbase", "telethon", "tweepy", "atproto",
                   "yt-dlp", "firecrawl-py", "curl_cffi", "fastapi", "uvicorn",
                   "beautifulsoup4", "cryptography"}
    assert not (names & server_only), (
        f"client dependencies include server-only packages: {sorted(names & server_only)}")
