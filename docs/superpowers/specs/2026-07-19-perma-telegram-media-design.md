# Perma.cc Telegram Media Retrieval — Design

**Date:** 2026-07-19  
**Status:** Approved for planning  
**Scope:** Fix Perma.cc retrieval of Telegram post captures (image + large video); generalize CSS `background-image` media extraction in `util.resolve_media`.

## Problem

Two Perma.cc Telegram captures fail to yield media:

| URL | Expected | Observed failure |
|-----|----------|------------------|
| `https://perma.cc/Z76P-4TE3` | 1 image | Text only; post photo never downloaded |
| `https://perma.cc/T5T9-QQ77` | 1 video | Empty / no media; inner replay iframe not ready |

### Root causes (verified)

1. **Telegram photos use CSS, not `<img>`.**  
   The post image lives on `<a class="tgme_widget_message_photo_wrap" style="…background-image:url('…')">`.  
   `_extract_media_elements` and `_inline_media_in_frame` only consider `<img>` / `<video>`. The channel avatar `<img>` (160×160) is inlined then dropped by the ≥256×256 filter.

2. **Innermost of the three Perma iframes is not reliably loaded.**  
   Structure: `iframe.archive-iframe` → `replay-web-page iframe` → `replay-app-main iframe`.  
   Current code times out at ~15s on the third iframe and falls back to an empty middle frame. Large WARCs (video ≈80 MB+) need ~40s+ before the innermost frame has content. That wait is the critical Perma.cc requirement.

3. **Nested Telegram embed (secondary).**  
   Media often sits one level deeper inside the archived page (`t.me/…?embed=1&mode=tme`). After the third iframe is ready, prefer the richest media-bearing descendant frame when present.

4. **Large video delivery.**  
   Past attempts to avoid Data-URIs failed in this codebase. Prefer trying Data-URI inlining again for session-bound replay URLs; keep in-frame `fetch` (with longer timeout) as fallback if Data-URI proves impractical for ~80 MB+.

## Goals

- `retrieve("https://perma.cc/Z76P-4TE3")` yields ≥1 image (post photo, not avatar).
- `retrieve("https://perma.cc/T5T9-QQ77")` yields ≥1 video.
- Existing Perma / other archiving tests remain green where they already pass.
- CSS `background-image` media works for any HTML path through `to_multimodal_sequence` / `resolve_media`, not only Perma.

## Non-goals

- New archiving services beyond Perma/util.
- Changing the ≥256×256 image size filter.
- Guaranteeing Data-URI success for arbitrarily large videos if memory/CDP limits block it (fallback required).

## Approach (approved)

**Approach 1 — Materialize into the existing pipeline:**

- Extend `util.resolve_media` to recognize eligible `background-image: url(...)` as image media.
- Harden Perma.cc so the **innermost of the three iframes** is fully loaded before extraction; then pick the best media frame if nested embeds exist.
- Retry Data-URI inlining for replay media; fall back to longer in-frame fetch when Data-URI fails or is skipped.

## Design

### 1. `util.resolve_media` — CSS background images

**File:** `scrapemm/util.py`

- Extend `_extract_media_elements` (or a helper it calls) to include elements whose inline `style` contains `background-image:url(...)`.
- Extract the URL; skip non-image / decorative cases via heuristics:
  - Accept: common image extensions, Telegram/CDN file URLs, replay-rewritten image URLs.
  - Skip: paths containing `/emoji/`, obvious icon sprites, empty/invalid URLs, vector-looking URLs (reuse `looks_like_vector_file_url` where applicable).
- URI wiring (explicit): either (a) set a synthetic `src` on the element to the extracted background URL before the existing `element.get("src")` path runs, or (b) extend the URI list builder in `resolve_media` to read background URLs when `src` is absent. Prefer (a) for minimal churn.
- Treat the element like an image for download / Data-URI resolution: after success, replace with the medium reference; on failure or too-small image, decompose as today.
- Keep the existing ≥256×256 filter so avatars and emoji backgrounds drop out.
- Do **not** parse every computed stylesheet rule—inline `style` only (matches Telegram widgets and keeps scope small).

### 2. Perma.cc — wait for innermost iframe, then best media frame

**File:** `scrapemm/integrations/perma_cc.py`

Priority order:

1. **Must:** Wait until the third iframe (`replay-app-main iframe`) has loaded usable document content. Do not fall back to the empty middle frame while the WARC is still loading. Use a longer deadline suitable for large captures (on the order of ~120s), polling for readiness (body present, meaningful HTML size / media / text) rather than a single short `wait_for_selector` timeout.
2. **Then:** If a descendant frame (e.g. Telegram embed) contains clearer media (`photo_wrap`, `video.tgme_widget_message_video`, etc.), prefer that frame for HTML export and inlining.
3. Call `_inline_media_in_frame` on the chosen frame before returning it as `ContentTarget`.

Cloudflare challenge handling stays as today.

### 3. Inlining and large video

**Files:** `scrapemm/integrations/perma_cc.py` (`_inline_media_in_frame`), possibly `scrapemm/util.py` (`_retrieve_media_bytes` timeout)

- Extend `_inline_media_in_frame` to also inline eligible CSS `background-image` URLs (same heuristics as util), so session/service-worker-bound replay URLs resolve inside the frame when possible.
- **Try Data-URI again** for images and videos within existing size caps (`MAX_IMAGE_BYTES` / `MAX_VIDEO_BYTES`).
- If Data-URI fails (timeout, memory, skip): leave the (replay-rewritten) URL in the DOM and rely on `resolve_media` → `fetch_*_via_page` with an **increased timeout** for large videos (target ~120–180s).
- Prefer the non-blurred Telegram `<video class="… js-message_video">` when both blurred and clear variants exist.

### 4. Testing

- Keep parametrized cases in `testing/test_archiving_site.py`:
  - `https://perma.cc/Z76P-4TE3` → `dict(image=1)`
  - `https://perma.cc/T5T9-QQ77` → `dict(video=1)`
- Add focused unit tests in `testing/test_utils.py` (or adjacent) for `background-image` extraction: photo-wrap style → image URI collected; emoji background → skipped.
- Manually/CI: video case may be slow; timeouts must allow WARC load + download.

## Error handling

- Perma: if the third iframe never becomes ready within the deadline, log and fall back through existing outer/middle paths (best-effort), returning whatever content exists rather than hanging forever.
- Inlining failures remain best-effort (`try`/`except` as today); extraction continues.
- Media that fails download is removed from the soup as today.

## Success criteria

1. Both Telegram Perma tests assert expected media counts.
2. Unit tests cover background-image accept/skip heuristics.
3. No regression on previously passing Perma cases in `test_archiving_site.py` (TikTok, screenshots, etc.) under normal conditions.

## Implementation notes

- Prefer simplicity; reuse existing download / size-filter / in-frame fetch paths.
- Python env: `C:/Users/mark/VEnvs/scrapeMM/Scripts/python.exe`.
- Data-URI is preferred for fidelity with Perma’s service worker; document any hard failure mode if large-video Data-URI is abandoned in favor of fetch-only.
