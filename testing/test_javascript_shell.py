from scrapemm.server.engine import _is_javascript_shell


def test_empty_app_shell_is_detected():
    """Reduced from https://youturn.in/factcheck/the-video-claiming-bangladeshi-fans-are-upset-over-indias-win-in-the-t20-world-cup-final-is-false.html,
    which a plain request answers with the title and an empty root only"""
    html = ('<html><head><title>Youturn | fact check</title><script>app()</script></head>'
            '<body><div id="root"></div><noscript>Enable JavaScript</noscript>'
            '<script>boot()</script></body></html>')
    assert _is_javascript_shell(html)


def test_page_with_text_or_media_is_no_shell():
    assert not _is_javascript_shell("<html><body><p>Hello</p></body></html>")
    assert not _is_javascript_shell('<html><body><img src="a.jpg"></body></html>')
    assert not _is_javascript_shell("<p>No body element at all</p>")
