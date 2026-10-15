"""Reddit posts come with their top comments; a link to a comment, with its thread."""

import pytest

from scrapemm.server.integrations import reddit
from scrapemm.server.integrations.reddit import Reddit, _chain_to, _joined, _render_comment

pytestmark = pytest.mark.server


def comment(id, replies=(), **data):
    return {"kind": "t1", "data": {"id": id, "author": f"user_{id}", "score": 1, "body": f"text {id}",
                                   "created_utc": 0, **data,
                                   "replies": {"data": {"children": list(replies)}} if replies else ""}}


MORE = {"kind": "more", "data": {"count": 99}}


@pytest.mark.parametrize("url, ids", [
    ("https://www.reddit.com/r/x/comments/1r11zgt/comment/o4np9xw/?js_challenge=1", ("1r11zgt", "o4np9xw")),
    ("https://www.reddit.com/r/x/comments/1r11zgt/some_title/o4np9xw/", ("1r11zgt", "o4np9xw")),
    ("https://www.reddit.com/r/x/comments/1r11zgt/some_title/", ("1r11zgt", None)),
    ("https://www.reddit.com/r/x/comments/1r11zgt/interesting/", ("1r11zgt", None)),
    ("https://www.reddit.com/r/x/comments/1r11zgt/", ("1r11zgt", None)),
    ("https://redd.it/1r11zgt", ("1r11zgt", None)),
])
async def test_the_post_and_comment_ids(url, ids):
    assert await Reddit._extract_ids(url, session=None) == ids


async def test_a_post_comes_with_its_top_comments_and_their_first_replies(monkeypatch):
    tree = [comment("bot", stickied=True), comment("auto", author="AutoModerator")]
    tree += [comment(f"c{i}", [comment(f"c{i}r{j}") for j in range(4)] + [MORE]) for i in range(12)]
    tree.append(MORE)
    parts = await Reddit()._render_comments({"num_comments": 500}, tree, None, session=None)
    text = "".join(p for p in parts if isinstance(p, str))
    assert "top 10 of 500" in text
    assert "user_bot" not in text and "user_auto" not in text
    assert "text c9\n" in text and "text c10" not in text  # Ten top comments
    assert "> text c0r1\n" in text and "c0r2" not in text  # Two replies each, quoted


async def test_a_linked_comment_comes_with_its_parents_and_replies():
    linked = comment("l", [comment("r1", [comment("r1a"), comment("r1b"), comment("r1c")])])
    tree = [comment("top", [comment("mid", [linked])])]
    assert [c["id"] for c in _chain_to(tree, "l")] == ["top", "mid", "l"]
    assert _chain_to(tree, "nope") is None
    parts = await Reddit()._render_comments({}, tree, "l", session=None)
    text = "".join(p for p in parts if isinstance(p, str))
    assert "below the 2 it replies to" in text
    assert "> > **u/user_l** · 1 point · 1970-01-01 00:00 UTC · **the linked comment**" in text
    assert "> > > text r1\n" in text and "> > > > text r1b\n" in text and "r1c" not in text


async def test_a_linked_comment_comes_with_its_top_replies_only():
    tree = [comment("l", [comment(f"r{i}") for i in range(12)])]
    parts = await Reddit()._render_comments({}, tree, "l", session=None)
    text = "".join(p for p in parts if isinstance(p, str))
    assert "> text r9\n" in text and "text r10" not in text


async def test_media_stand_where_the_comment_references_them(monkeypatch):
    downloaded = []

    class Medium:  # Stands in for the ezMM item
        def __init__(self, n):
            self.n = n

    async def fake_image(url, **kwargs):
        downloaded.append(url)
        return Medium(len(downloaded))

    monkeypatch.setattr(reddit, "download_image", fake_image)
    body = ("Look: https://preview.redd.it/abc123.jpeg?width=10&s=x and\n"
            "![gif](giphy|XYZ) but [a link](https://example.com) stays")
    metadata = {"abc123": {"e": "Image", "s": {"u": "https://preview.redd.it/abc123.jpeg?full"}}}
    parts = _joined(await _render_comment(comment("m", body=body, media_metadata=metadata)["data"],
                                          1, False, session=None))
    assert downloaded == ["https://preview.redd.it/abc123.jpeg?full",
                          "https://media.giphy.com/media/XYZ/giphy.gif"]
    assert [p.n for p in parts if isinstance(p, Medium)] == [1, 2]
    text = "".join(p for p in parts if isinstance(p, str))
    assert "> Look: " in text and "[a link](https://example.com) stays" in text
    assert "preview.redd.it" not in text and "giphy|" not in text
