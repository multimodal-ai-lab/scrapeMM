"""Builds the default URL suite of the web UI's Test page from the retrieval tests.

Every test that checks a retrieval with `assert_expectations(result, expected)` holds a
list of URLs, each with the number of images and videos it must at least yield. This
collects them -- with the test they come from as the category -- into
`scrapemm/server/test_suite_default.json`. Commented-out cases are skipped by nature,
since they are not part of the syntax tree.

    python scripts/build_test_suite.py
"""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ["test_social_media.py", "test_archiving_site.py", "test_retrieval.py"]
TARGET = ROOT / "scrapemm" / "server" / "test_suite_default.json"


def literal_dict(node) -> dict | None:
    """dict(image=1) or {"image": 1}, evaluated; None for anything else."""
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "dict" and not node.args:
        return {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
    if isinstance(node, ast.Dict):
        return ast.literal_eval(node)
    return None


NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "x": "X (Twitter)", "perma_cc": "Perma.cc",
         "archive_today": "Archive.today", "internet_archive": "Internet Archive",
         "generic_retrieval": "Open web", "other_archiving_services": "Other archives"}


def category(name: str) -> str:
    """test_archive_today -> "Archive.today"."""
    key = name.removeprefix("test_")
    return NAMES.get(key, key.replace("_", " ").capitalize())


def cases(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        # Only retrieval tests: those that check what a retrieval yielded
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "assert_expectations"]
        if not calls:
            continue
        # An expectation fixed in the body (e.g. every YouTube URL must give a video)
        fixed = literal_dict(calls[0].args[1]) if len(calls[0].args) > 1 else None

        for decorator in fn.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and getattr(decorator.func, "attr", None) == "parametrize"):
                continue
            names = [n.strip() for n in ast.literal_eval(decorator.args[0]).split(",")]
            if "url" not in names:
                continue
            for entry in decorator.args[1].elts:
                values = entry.elts if isinstance(entry, ast.Tuple) else [entry]
                row = dict(zip(names, values))
                try:
                    url = ast.literal_eval(row["url"])
                except ValueError:
                    continue
                expected = literal_dict(row["expected"]) if "expected" in row else fixed
                yield {"url": url, "expected": expected or {}, "category": category(fn.name)}


def main():
    suite, seen = [], set()
    for source in SOURCES:
        for case in cases(ROOT / "testing" / source):
            if case["url"] not in seen:
                seen.add(case["url"])
                suite.append(case)
    TARGET.write_text(json.dumps(suite, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(suite)} URLs in {len({c['category'] for c in suite})} categories -> {TARGET}")


if __name__ == "__main__":
    main()
