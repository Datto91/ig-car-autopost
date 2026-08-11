"""Caption construction, including best-effort attribution.

Attribution problem, stated plainly: the IG Hashtag Search API does not return
the author's username. Meta's docs are explicit -- "You cannot request the
username field on returned media objects" -- and the `user_id` field it does
return is the *querying* user's id, not the author's. The oEmbed endpoint no
longer returns `author_name` either, and its terms forbid persisting metadata.

So the only handle recoverable at zero cost is one the creator typed into their
own caption (self-tags, photographer credits, "shot by @x"). That is what
extract_handle does. It succeeds on a meaningful fraction of posts and fails
silently on the rest, in which case no credit line is emitted.
"""

from __future__ import annotations

import hashlib
import logging
import re

log = logging.getLogger(__name__)

# @handle: 1-30 chars, letters/digits/._ per Instagram's username rules.
_HANDLE_RE = re.compile(r"@([A-Za-z0-9._]{1,30})")

# Phrases that mark the handle right after them as the photographer/owner.
# Ordered strongest-signal first.
_CREDIT_CUES = (
    "shot by",
    "photo by",
    "pic by",
    "captured by",
    "credit to",
    "credits to",
    "credit",
    "credits",
    "cr.",
    "cr:",
    "\U0001F4F8",  # camera-with-flash emoji, the de facto IG credit marker
    "\U0001F4F7",  # camera emoji
    "by",
    "owner",
    "car by",
)

# Handles that are never a creator credit.
_HANDLE_DENY = {
    "instagram",
    "explore",
    "reels",
    "shop",
    "highlights",
    "instagood",
}


def _clean(handle: str) -> str:
    return handle.strip().strip(".").strip("_").lower()


def extract_handle(caption: str | None) -> str | None:
    """Best-effort author handle from the source caption.

    Strategy, highest confidence first:
      1. A handle immediately following an explicit credit cue.
      2. The single handle in the caption, if there is exactly one.
      3. Nothing -- returns None, and the caller omits the credit line.

    Returns the handle WITHOUT the leading @, or None.
    """
    if not caption:
        return None

    lowered = caption.lower()

    # 1. Credit cue followed closely by a handle.
    for cue in _CREDIT_CUES:
        start = 0
        while True:
            idx = lowered.find(cue, start)
            if idx == -1:
                break
            # Look at the ~40 chars after the cue for a handle.
            window = caption[idx + len(cue) : idx + len(cue) + 40]
            match = _HANDLE_RE.search(window)
            if match:
                handle = _clean(match.group(1))
                if handle and handle not in _HANDLE_DENY:
                    return handle
            start = idx + len(cue)

    # 2. Exactly one handle in the whole caption -> almost always the creator
    #    or the featured owner.
    handles = [_clean(h) for h in _HANDLE_RE.findall(caption)]
    handles = [h for h in handles if h and h not in _HANDLE_DENY]
    unique = list(dict.fromkeys(handles))
    if len(unique) == 1:
        return unique[0]

    return None


def _pick_opener(openers: list[str], media_id: str) -> str:
    """Deterministic per-post opener.

    Deterministic rather than random so a re-run of the same workflow produces
    the same caption (matters for resumes and for debugging).
    """
    if not openers:
        return ""
    digest = hashlib.sha256(media_id.encode("utf-8")).digest()
    return openers[digest[0] % len(openers)]


def count_hashtags(text: str) -> int:
    return len(re.findall(r"#\w+", text))


def build_caption(cfg: dict, candidate: dict) -> str:
    """Assemble the final caption for a candidate.

    Layout:
        <opener>
        <credit line, if a handle was recovered>
        <hashtags>
    """
    cap_cfg = cfg.get("caption") or {}
    media_id = candidate.get("media_id", "")

    parts: list[str] = []

    opener = _pick_opener(cap_cfg.get("openers") or [], media_id)
    if opener:
        parts.append(opener)

    handle = candidate.get("credit_handle")
    if handle:
        template = cap_cfg.get("credit_template") or "\U0001F4F8 {handle}"
        parts.append(template.format(handle=f"@{handle}"))
    else:
        fallback = (cap_cfg.get("credit_fallback") or "").strip()
        if fallback:
            parts.append(fallback)

    tags = cap_cfg.get("hashtags") or []
    if tags:
        parts.append(" ".join(f"#{t.lstrip('#')}" for t in tags))

    caption = "\n\n".join(p for p in parts if p)

    max_len = int(cap_cfg.get("max_length", 2200))
    if len(caption) > max_len:
        caption = caption[: max_len - 1].rstrip() + "…"

    # Instagram rejects captions with more than 30 hashtags.
    if count_hashtags(caption) > 30:
        log.warning("caption for %s exceeds 30 hashtags; trimming", media_id)
        seen = 0

        def _trim(match: re.Match) -> str:
            nonlocal seen
            seen += 1
            return match.group(0) if seen <= 30 else ""

        caption = re.sub(r"#\w+", _trim, caption)

    return caption.strip()
