"""How media gets from the server to the client -- and, on one machine, how it doesn't.

The requirement these guard: when the client runs beside the server, the media it
receives must be the very files the server already saved, not copies of them. A lab
that retrieves thousands of videos should not end up with two of each.
"""

import pytest
from ezmm.common.registry import item_registry

from scrapemm.client import media as media_module
from scrapemm.common.wire import ContentPayload, ItemDescriptor, RegistryInfo

FINGERPRINT = "abc123"


@pytest.fixture(autouse=True)
def clear_mode_cache():
    media_module._mode_cache.clear()
    yield
    media_module._mode_cache.clear()


@pytest.fixture
def server_registry(tmp_path):
    """A directory that looks like another scrapeMM's media registry."""
    root = tmp_path / "server_media"
    (root / "image").mkdir(parents=True)
    (root / media_module.FINGERPRINT_FILENAME).write_text(FINGERPRINT, encoding="utf-8")
    return root


def test_same_registry_is_recognised_as_shared(monkeypatch, server_registry):
    """Client and server sharing one registry means the ids already match; nothing at
    all needs to happen to the sequence."""
    monkeypatch.setattr(item_registry, "path", server_registry, raising=False)
    info = RegistryInfo(root=str(server_registry), fingerprint=FINGERPRINT,
                        writable_by_client=True)
    assert media_module.resolve_mode(info) == "shared"


def test_reachable_foreign_registry_is_linked(monkeypatch, tmp_path, server_registry):
    """A different registry on the same filesystem: adopt the files where they lie."""
    monkeypatch.setattr(item_registry, "path", tmp_path / "client_media", raising=False)
    info = RegistryInfo(root=str(server_registry), fingerprint=FINGERPRINT,
                        writable_by_client=True)
    assert media_module.resolve_mode(info) == "link"


def test_unreachable_registry_falls_back_to_download(monkeypatch, tmp_path):
    monkeypatch.setattr(item_registry, "path", tmp_path / "client_media", raising=False)
    info = RegistryInfo(root="/not/here", fingerprint=FINGERPRINT,
                        writable_by_client=True)
    assert media_module.resolve_mode(info) == "download"


def test_fingerprint_mismatch_is_not_linked(monkeypatch, tmp_path, server_registry):
    """A path that happens to exist locally but holds a *different* registry must not
    be mistaken for the server's -- that would hand out unrelated files."""
    monkeypatch.setattr(item_registry, "path", tmp_path / "client_media", raising=False)
    info = RegistryInfo(root=str(server_registry), fingerprint="something-else",
                        writable_by_client=True)
    assert media_module.resolve_mode(info) == "download"


def test_server_that_hides_its_paths_forces_download(monkeypatch, tmp_path):
    monkeypatch.setattr(item_registry, "path", tmp_path / "client_media", raising=False)
    assert media_module.resolve_mode(RegistryInfo()) == "download"


def test_explicit_preference_wins(monkeypatch, server_registry):
    """A deployment that says 'always download' is obeyed even where linking would
    work, because the reason may be one this code cannot see (a read-only mount, a
    server that prunes)."""
    monkeypatch.setattr(item_registry, "path", server_registry, raising=False)
    info = RegistryInfo(root=str(server_registry), fingerprint=FINGERPRINT,
                        writable_by_client=True)
    assert media_module.resolve_mode(info, preference="download") == "download"


async def test_shared_mode_keeps_references_untouched(monkeypatch, server_registry):
    monkeypatch.setattr(item_registry, "path", server_registry, raising=False)
    content = ContentPayload(
        multimodal="before <image:7> after",
        items=[ItemDescriptor(ref="<image:7>", kind="image", id=7)])

    # No session or base URL is needed: shared mode must not touch the network
    sequence = await media_module.resolve_content(
        content, RegistryInfo(root=str(server_registry), fingerprint=FINGERPRINT,
                              writable_by_client=True),
        session=None, base_url="", headers={}, preference="shared")
    assert "<image:7>" in str(sequence)


def test_remap_rewrites_every_reference_once():
    """Ids are remapped in a single pass: a new id that collides with an old one must
    not be rewritten a second time."""
    class FakeItem:
        def __init__(self, reference):
            self.reference = reference

    text = "a <image:1> b <image:2> c"
    items = {"<image:1>": FakeItem("<image:2>"), "<image:2>": FakeItem("<image:9>")}
    assert media_module._remap(text, items) == "a <image:2> b <image:9> c"


def test_remap_drops_media_that_could_not_be_obtained():
    """A dangling reference would make MultimodalSequence refuse the whole sequence,
    taking the text down with it. Losing one image beats losing the page."""
    assert media_module._remap("a <image:1> b", {"<image:1>": None}) == "a  b"


def test_remap_leaves_unknown_references_alone():
    """A reference with no manifest entry is not ours to rewrite or drop."""
    assert media_module._remap("a <image:5> b", {}) == "a <image:5> b"
