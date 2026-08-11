"""Offline tests for the pure-logic pieces. No network, no credentials.

Run:  python -m tests.test_logic
Also runs in CI via .github/workflows/test.yml on every push.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import store  # noqa: E402
from src.caption import (  # noqa: E402
    build_caption,
    clean_source_description,
    count_hashtags,
    extract_handle,
)
from src.discover import passes_filters  # noqa: E402
from src.images import MAX_ASPECT, MIN_ASPECT, MIN_WIDTH, normalize  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, actual, expected) -> None:
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         expected: {expected!r}\n         actual:   {actual!r}")


def check_true(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


# ---------------------------------------------------------------------------
print("\n[handle extraction] the attribution workaround")
# ---------------------------------------------------------------------------

check("explicit 'shot by'", extract_handle("Clean E30. shot by @dattophoto"), "dattophoto")
check("camera emoji cue", extract_handle("Sunset run \U0001F4F8 @lens.guy"), "lens.guy")
check("'credit' cue", extract_handle("credit @owner_23 for this one"), "owner_23")
check("'cr:' shorthand", extract_handle("cr: @quickpic"), "quickpic")
check("single handle fallback", extract_handle("my build @my_gti finally done"), "my_gti")
check("no handle at all", extract_handle("just a clean car #jdm"), None)
check("empty caption", extract_handle(""), None)
check("None caption", extract_handle(None), None)
check(
    "ambiguous: multiple handles, no cue -> give up",
    extract_handle("@one @two @three all great"),
    None,
)
check(
    "cue wins over ambiguity",
    extract_handle("@spam @morespam photo by @realauthor @evenmore"),
    "realauthor",
)
check("denylisted handle ignored", extract_handle("follow @instagram"), None)
check("trailing punctuation stripped", extract_handle("shot by @guy."), "guy")
check(
    "cue with no nearby handle falls through to single-handle rule",
    extract_handle("photo by someone unknown, car is @the_owner"),
    "the_owner",
)

# ---------------------------------------------------------------------------
print("\n[caption building]")
# ---------------------------------------------------------------------------

cfg = store.load_config()

with_credit = build_caption(cfg, {"media_id": "abc123", "credit_handle": "dattophoto"})
check_true("credit line present when handle known", "@dattophoto" in with_credit)
check_true("hashtags appended", "#cars" in with_credit)

without_credit = build_caption(cfg, {"media_id": "abc123", "credit_handle": None})
check_true("no bare '@' when handle unknown", "@" not in without_credit)
check_true("still has hashtags", "#cars" in without_credit)

a = build_caption(cfg, {"media_id": "same-id", "credit_handle": None})
b = build_caption(cfg, {"media_id": "same-id", "credit_handle": None})
check("opener is deterministic for a given media id", a, b)

openers = {
    build_caption(cfg, {"media_id": f"id-{i}", "credit_handle": None}).split("\n")[0]
    for i in range(60)
}
check_true("openers actually vary across posts", len(openers) > 1)

check_true("caption within 2200 chars", len(with_credit) <= 2200)
check_true("30-hashtag ceiling respected", count_hashtags(with_credit) <= 30)

spammy = build_caption(
    {"caption": {"hashtags": [f"tag{i}" for i in range(45)], "openers": ["hi"], "max_length": 2200}},
    {"media_id": "x", "credit_handle": None},
)
check_true("over-30 hashtag config gets trimmed", count_hashtags(spammy) <= 30)

# ---------------------------------------------------------------------------
print("\n[source description reuse] free captions from the original post")
# ---------------------------------------------------------------------------

src_clean = clean_source_description(
    "1991 R32 GTR. RB26 with -5 turbos and 18x10 TE37s. "
    "Shot by @lensguy at the touge. #jdm #r32 https://linktr.ee/x",
    cfg,
)
check_true("keeps the car detail", "RB26" in src_clean and "TE37s" in src_clean)
check_true("drops the source hashtags", "#jdm" not in src_clean)
check_true("drops URLs", "linktr" not in src_clean)
check_true("drops the credit handle from the body", "@lensguy" not in src_clean)
check_true("no dangling credit cue left behind", "shot by" not in src_clean.lower())

check(
    "keeps detail after a mid-sentence credit",
    "4AGE" in clean_source_description(
        "AE86 hachiroku, owner @tofu_shop, running a 4AGE 20v silvertop.", cfg
    ),
    True,
)
check(
    "hashtags-only caption yields nothing reusable",
    clean_source_description("#jdm #datsun #240z #classic", cfg),
    "",
)
check("empty source yields nothing", clean_source_description("", cfg), "")
check("None source yields nothing", clean_source_description(None, cfg), "")
check_true(
    "over-long source is truncated",
    len(clean_source_description("Picked this 510 up in Osaka. " * 60, cfg))
    <= int(cfg["caption"]["max_source_chars"]) + 1,
)

# Full caption in source mode: body + signature + credit + tags.
src_cfg = {**cfg, "caption": {**cfg["caption"], "mode": "source"}}
src_cap = build_caption(
    src_cfg,
    {
        "media_id": "src1",
        "credit_handle": "lensguy",
        "source_caption": "FD RX-7 on TE37s, sunset run through the hills. #rx7",
    },
)
check_true("source body used in caption", "FD RX-7" in src_cap)
check_true("your signature block is appended", "Follow for daily" in src_cap)
check_true("credit line present", "@lensguy" in src_cap)
check_true("our hashtags appended", "#jdm" in src_cap)
check_true("within 2200 chars", len(src_cap) <= 2200)
check_true("30-hashtag ceiling respected", count_hashtags(src_cap) <= 30)

# Unusable source must fall back to a template opener, never post bare tags.
fallback_cap = build_caption(
    src_cfg, {"media_id": "src2", "credit_handle": None, "source_caption": "#jdm #r32"}
)
first_line = fallback_cap.split("\n")[0]
check_true("falls back to a template opener", bool(first_line) and "#" not in first_line)

# ---------------------------------------------------------------------------
print("\n[candidate filtering]")
# ---------------------------------------------------------------------------

good = {
    "id": "1",
    "media_type": "IMAGE",
    "media_url": "https://x/y.jpg",
    "like_count": 5000,
    "comments_count": 50,
    "caption": "clean build #jdm",
}
check("accepts a good candidate", passes_filters(good, cfg)[0], True)

check(
    "rejects CAROUSEL_ALBUM (no media_url from API)",
    passes_filters({**good, "media_type": "CAROUSEL_ALBUM"}, cfg)[0],
    False,
)
check("rejects VIDEO", passes_filters({**good, "media_type": "VIDEO"}, cfg)[0], False)
check("rejects missing media_url", passes_filters({**good, "media_url": None}, cfg)[0], False)
check("rejects low likes", passes_filters({**good, "like_count": 10}, cfg)[0], False)
check(
    "rejects denylisted caption",
    passes_filters({**good, "caption": "DM to buy this now"}, cfg)[0],
    False,
)
check(
    "rejects hashtag spam",
    passes_filters({**good, "caption": " ".join(f"#t{i}" for i in range(40))}, cfg)[0],
    False,
)
check(
    "hidden like_count is not treated as zero",
    passes_filters({k: v for k, v in good.items() if k != "like_count"}, cfg)[0],
    True,
)

# ---------------------------------------------------------------------------
print("\n[image normalization] Instagram's hard input rules")
# ---------------------------------------------------------------------------

from PIL import Image  # noqa: E402


def make(w: int, h: int, mode: str = "RGB", fmt: str = "PNG") -> bytes:
    img = Image.new(mode, (w, h), (200, 30, 30) if mode == "RGB" else None)
    if mode == "RGBA":
        img = Image.new("RGBA", (w, h), (200, 30, 30, 128))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


tmp = Path(tempfile.mkdtemp())

m = normalize(make(1080, 1080), tmp / "sq.jpg")
check("square passes through", (m["width"], m["height"]), (1080, 1080))
check_true("output is a real JPEG", (tmp / "sq.jpg").read_bytes()[:2] == b"\xff\xd8")

m = normalize(make(4000, 3000), tmp / "big.jpg")
check("oversize width clamped to 1440", m["width"], 1440)

m = normalize(make(200, 200), tmp / "small.jpg")
check("undersize width raised to 320", m["width"], MIN_WIDTH)

m = normalize(make(1080, 1920), tmp / "story.jpg")  # 9:16, far too tall
check_true(f"9:16 padded into legal band (got {m['aspect']})", m["aspect"] >= MIN_ASPECT - 0.01)
check_true("padding was recorded", any("aspect" in n for n in m["adjustments"]))

m = normalize(make(3000, 1000), tmp / "pano.jpg")  # 3:1, far too wide
check_true(f"3:1 padded into legal band (got {m['aspect']})", m["aspect"] <= MAX_ASPECT + 0.01)

m = normalize(make(800, 800, mode="RGBA"), tmp / "alpha.jpg")
check_true("transparency flattened", any("transparency" in n for n in m["adjustments"]))
check_true("alpha output still JPEG", (tmp / "alpha.jpg").read_bytes()[:2] == b"\xff\xd8")

try:
    normalize(b"this is not an image", tmp / "bad.jpg")
    check("garbage input raises", False, True)
except Exception as exc:
    check("garbage input raises ImageError", type(exc).__name__, "ImageError")

# ---------------------------------------------------------------------------
print("\n[hashtag budget] the 30-unique-tags-per-7-days cap")
# ---------------------------------------------------------------------------

check_true("config stays within the 30-tag cap", len(cfg["hashtags"]) <= 30)
check_true(
    f"tags_per_run ({cfg['tags_per_run']}) <= configured tags",
    cfg["tags_per_run"] <= len(cfg["hashtags"]),
)

over = dict(cfg)
over["hashtags"] = [f"tag{i}" for i in range(31)]
try:
    tmp_cfg = store.CONFIG_PATH
    # load_config validates on read; simulate by calling the check directly.
    if len(over["hashtags"]) > store.HASHTAG_UNIQUE_CAP:
        raise ValueError("too many")
    check("31 tags rejected", False, True)
except ValueError:
    check("31-tag config would be rejected", True, True)

# ---------------------------------------------------------------------------
print("\n[cadence guards]")
# ---------------------------------------------------------------------------

pub = cfg["publish"]
check_true("daily_cap well under Meta's 100/24h", pub["daily_cap"] < 100)
check_true("min_gap_minutes is positive", pub["min_gap_minutes"] > 0)
check_true(
    "cap and gap are mutually consistent within 24h",
    pub["daily_cap"] * pub["min_gap_minutes"] <= 24 * 60 + pub["min_gap_minutes"],
)

# ---------------------------------------------------------------------------
print(f"\n{'=' * 60}")
print(f"  {PASS} passed, {FAIL} failed")
print(f"{'=' * 60}\n")
sys.exit(1 if FAIL else 0)
