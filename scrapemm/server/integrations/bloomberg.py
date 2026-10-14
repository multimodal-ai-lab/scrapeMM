"""Bloomberg articles from the page data of their markup (`__NEXT_DATA__`): the story,
body included, which the page renders by script -- and which a Wayback capture keeps even
where its replay shows only the paywall. Plain HTML of the article, for `to_scraped_content`."""
import html as html_lib
import json
import re
from typing import Optional

_NEXT_DATA = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Body nodes that carry nothing of the article
_SKIP = {"ad", "inline-newsletter", "inline-recirc", "newsletter", "footnoteRef"}


def _esc(text) -> str:
    return html_lib.escape(str(text or ""), quote=True)


def _link_href(data: dict) -> Optional[str]:
    """The web address a link or entity node points to (`bbg://` terminal links are of
    no use outside a Bloomberg Terminal)."""
    link = data.get("link") or data
    href = (link.get("destination") or {}).get("web") or link.get("webUrl") \
        or link.get("data-web-url") or link.get("href")
    return href if href and href.startswith("http") else None


def _inline(nodes: list) -> str:
    out = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        inner = _inline(node.get("content"))
        if kind == "text":
            out.append(_esc(node.get("value")))
        elif kind == "br":
            out.append("<br>")
        elif kind in ("link", "entity"):
            href = _link_href(node.get("data") or {})
            out.append(f'<a href="{_esc(href)}">{inner}</a>' if href else inner)
        elif kind in ("strong", "bold"):
            out.append(f"<strong>{inner}</strong>")
        elif kind in ("emphasis", "em", "italic"):
            out.append(f"<em>{inner}</em>")
        else:  # Unknown inline wrapper: keep its text
            out.append(inner)
    return "".join(out)


def _media(node: dict) -> str:
    data = node.get("data") or {}
    sub = node.get("subType")
    item = data.get(sub) or {}
    attachment = data.get("attachment") or {}
    caption = " ".join(filter(None, [item.get("caption") or attachment.get("caption"),
                                     item.get("credit")]))
    if sub == "photo":
        src = item.get("src") or attachment.get("url")
        if not src:
            return ""
        return (f'<figure><img src="{_esc(src)}" alt="{_esc(item.get("alt"))}">'
                f'<figcaption>{_esc(caption)}</figcaption></figure>')
    if sub == "chart":
        title = " – ".join(filter(None, [attachment.get("title"), attachment.get("subtitle")]))
        notes = " ".join(filter(None, [attachment.get("footnote"), attachment.get("source")]))
        img = f'<img src="{_esc(item["fallback"])}" alt="{_esc(title)}">' if item.get("fallback") else ""
        return f"<figure>{img}<figcaption>Chart: {_esc(title)} {_esc(notes)}</figcaption></figure>"
    if sub == "video":
        title = attachment.get("title") or ""
        return f"<p><em>[Video: {_esc(title)}] {_esc(caption)}</em></p>"
    return ""


def _block(node: dict) -> str:
    kind = node.get("type")
    if kind in _SKIP or not isinstance(node, dict):
        return ""
    data = node.get("data") or {}
    content = node.get("content") or []
    if kind == "paragraph":
        text = _inline(content)
        return f"<p>{text}</p>" if text.strip() else ""
    if kind == "heading":
        level = min(max(int(data.get("level") or 2), 2), 6)
        return f"<h{level}>{_inline(content)}</h{level}>"
    if kind in ("quote", "blockquote", "pullquote"):
        return f"<blockquote>{''.join(_block(c) if c.get('type') != 'text' else _inline([c]) for c in content)}</blockquote>"
    if kind == "list":
        tag = "ol" if data.get("style") in ("number", "ordered", "ol") else "ul"
        return f"<{tag}>{''.join(f'<li>{_children(c)}</li>' for c in content)}</{tag}>"
    if kind == "media":
        return _media(node)
    if kind == "tabularData":  # E.g. a "Read More" box: its title, then its cells
        cols = next((c for c in content if c.get("type") == "columns"), {})
        titles = [d.get("title") for d in (cols.get("data") or {}).get("definitions", []) if d.get("title")]
        cells = [_children(cell) for row in content if row.get("type") == "row"
                 for cell in row.get("content") or []]
        head = f"<p><strong>{_esc(' | '.join(titles))}</strong></p>" if titles else ""
        return head + "".join(cells)
    return _children(node)  # Unknown block: its children


def _children(node: dict) -> str:
    content = node.get("content") or []
    if content and all(c.get("type") in ("text", "entity", "link", "br", "strong", "emphasis")
                       for c in content if isinstance(c, dict)):
        return _inline(content)
    return "".join(_block(c) for c in content if isinstance(c, dict))


def bloomberg_story(markup: str) -> Optional[dict]:
    """The `story` of a Bloomberg article page's __NEXT_DATA__, if it has a body."""
    match = _NEXT_DATA.search(markup)
    if not match:
        return None
    try:
        story = json.loads(match.group(1))["props"]["pageProps"]["story"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(story, dict) or not (story.get("body") or {}).get("content"):
        return None
    return story


def bloomberg_article_html(markup: str) -> Optional[str]:
    """The article of a Bloomberg page as plain HTML (headline, byline, date, lede image,
    body), read from its __NEXT_DATA__. None if the markup holds no story body."""
    story = bloomberg_story(markup)
    if story is None:
        return None
    parts = [f"<h1>{_esc(story.get('headline'))}</h1>"]
    # The bullet points above some articles (`summary` mostly repeats the first paragraph)
    if bullets := [b for b in story.get("abstract") or [] if isinstance(b, str) and b.strip()]:
        parts.append("<ul>" + "".join(f"<li>{_esc(b)}</li>" for b in bullets) + "</ul>")
    byline = story.get("byline") or ", ".join(a.get("name", "") for a in story.get("authors") or [])
    meta = " · ".join(filter(None, [f"By {byline}" if byline else "",
                                    story.get("publishedAt"),
                                    f"updated {story['updatedAt']}" if story.get("updatedAt")
                                    and story.get("updatedAt") != story.get("publishedAt") else ""]))
    if meta:
        parts.append(f"<p>{_esc(meta)}</p>")
    lede = story.get("lede") or {}
    if lede.get("url") or story.get("ledeImageUrl"):
        caption = " ".join(filter(None, [lede.get("caption"), lede.get("credit")]))
        parts.append(f'<figure><img src="{_esc(lede.get("url") or story["ledeImageUrl"])}" '
                     f'alt="{_esc(lede.get("alt"))}"><figcaption>{_esc(caption)}</figcaption></figure>')
    parts += [_block(node) for node in story["body"]["content"] if isinstance(node, dict)]
    title = _esc(story.get("headline"))
    return f"<html><head><title>{title}</title></head><body><article>{''.join(parts)}</article></body></html>"


