"""Discovery: find candidate posts via hashtag search, re-host, queue them.

Run on a cron several times a day. Every candidate is written to queue/pending
along with a normalized JPEG in media/, so the publish step never depends on a
short-lived Meta CDN URL.

Usage:
    python -m src.discover [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import store
from .caption import build_caption, count_hashtags, extract_handle
from .ig_api import IGError, InstagramClient, RateLimited
from .images import ImageError, fetch_and_normalize

log = logging.getLogger("discover")


def passes_filters(media: dict, cfg: dict) -> tuple[bool, str]:
    """Gate a raw media object. Returns (ok, reason_if_rejected)."""
    f = cfg.get("filters") or {}

    mtype = media.get("media_type")
    allowed = f.get("allowed_media_types") or ["IMAGE"]
    if mtype not in allowed:
        return False, f"media_type {mtype} not in {allowed}"

    # CAROUSEL_ALBUM never carries a media_url; IMAGE should.
    if not media.get("media_url"):
        return False, "no media_url"

    likes = media.get("like_count")
    # like_count is omitted when the owner hides like counts -- treat missing as
    # unknown rather than zero, and let it through on other signals.
    if likes is not None and likes < int(f.get("min_like_count", 0)):
        return False, f"like_count {likes} below minimum"

    comments = media.get("comments_count")
    if comments is not None and comments < int(f.get("min_comments_count", 0)):
        return False, f"comments_count {comments} below minimum"

    caption = (media.get("caption") or "").lower()
    for bad in f.get("caption_denylist") or []:
        if bad.lower() in caption:
            return False, f"caption matched denylist term {bad!r}"

    max_tags = int(f.get("max_caption_hashtags", 30))
    if count_hashtags(caption) > max_tags:
        return False, f"caption has more than {max_tags} hashtags (spam signal)"

    return True, ""


def discover(limit: int | None = None, dry_run: bool = False) -> dict:
    cfg = store.load_config()
    store.ensure_dirs()

    ig_user_id = os.environ.get("IG_USER_ID")
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not ig_user_id or not token:
        raise SystemExit("IG_USER_ID and IG_ACCESS_TOKEN must be set in the environment")

    client = InstagramClient(ig_user_id, token)

    budget = store.hashtag_budget()
    log.info(
        "hashtag budget: %d/%d unique tags used in trailing 7 days (%d remaining)",
        budget["used"],
        budget["cap"],
        budget["remaining"],
    )

    tags_per_run = int(cfg.get("tags_per_run", 5))
    tags = store.pick_tags(cfg, tags_per_run)
    if not tags:
        log.error(
            "no queryable hashtags: the 30-unique-tags/7-day cap is exhausted. "
            "Tags in window: %s",
            ", ".join(budget["tags"]),
        )
        return {"queued": 0, "reason": "hashtag budget exhausted"}

    log.info("querying tags: %s", ", ".join(f"#{t}" for t in tags))

    already_seen = store.seen_ids()
    use_top = bool(cfg.get("use_top_media", True))

    queued = 0
    newly_seen: list[str] = []
    rejected: dict[str, int] = {}
    max_queue = limit if limit is not None else tags_per_run * 3

    for tag in tags:
        if queued >= max_queue:
            break

        # Resolve id (cached forever -- ids are static and global).
        hid = store.cached_hashtag_id(tag)
        try:
            if not hid:
                hid = client.hashtag_id(tag)
                store.cache_hashtag_id(tag, hid)
                log.info("resolved #%s -> %s", tag, hid)
            store.record_hashtag_query(tag)
        except RateLimited as exc:
            log.error("throttled resolving #%s: %s -- stopping run", tag, exc)
            break
        except IGError as exc:
            log.error("could not resolve #%s: %s", tag, exc)
            continue

        # Fetch media. top_media is not limited to the last 24h, so it is the
        # better default; recent_media backfills when top_media is thin.
        try:
            media_list = (
                client.hashtag_top_media(hid) if use_top else client.hashtag_recent_media(hid)
            )
            if len(media_list) < 5:
                extra = (
                    client.hashtag_recent_media(hid)
                    if use_top
                    else client.hashtag_top_media(hid)
                )
                media_list.extend(extra)
        except RateLimited as exc:
            log.error("throttled fetching media for #%s: %s -- stopping run", tag, exc)
            break
        except IGError as exc:
            log.error("could not fetch media for #%s: %s", tag, exc)
            continue

        log.info("#%s returned %d media objects", tag, len(media_list))

        for media in media_list:
            if queued >= max_queue:
                break

            mid = media.get("id")
            if not mid or mid in already_seen:
                continue

            ok, reason = passes_filters(media, cfg)
            if not ok:
                rejected[reason.split(" ")[0]] = rejected.get(reason.split(" ")[0], 0) + 1
                log.debug("skip %s: %s", mid, reason)
                # Mark seen so we do not re-evaluate the same reject forever.
                newly_seen.append(mid)
                continue

            source_caption = media.get("caption") or ""
            handle = extract_handle(source_caption)

            candidate = {
                "media_id": mid,
                "source_tag": tag,
                "permalink": media.get("permalink"),
                "source_media_url": media.get("media_url"),
                "source_caption": source_caption,
                "credit_handle": handle,
                "like_count": media.get("like_count"),
                "comments_count": media.get("comments_count"),
                "source_timestamp": media.get("timestamp"),
                "discovered_at": store.iso(store.utcnow()),
            }
            candidate["caption"] = build_caption(cfg, candidate)

            if dry_run:
                log.info(
                    "[dry-run] would queue %s from #%s (credit=%s)",
                    mid,
                    tag,
                    f"@{handle}" if handle else "none recovered",
                )
                queued += 1
                newly_seen.append(mid)
                continue

            # Re-host now: the CDN url will be dead by publish time.
            dest = store.MEDIA_DIR / f"{mid}.jpg"
            try:
                image_meta = fetch_and_normalize(candidate["source_media_url"], dest)
            except ImageError as exc:
                log.warning("could not process image for %s: %s", mid, exc)
                newly_seen.append(mid)
                continue

            candidate["image"] = image_meta
            path = store.queue_candidate(candidate)
            queued += 1
            newly_seen.append(mid)
            log.info(
                "queued %s from #%s -> %s (credit=%s, %dx%d)",
                mid,
                tag,
                path.name,
                f"@{handle}" if handle else "none recovered",
                image_meta["width"],
                image_meta["height"],
            )

    if newly_seen and not dry_run:
        store.mark_seen(newly_seen)

    if rejected:
        log.info("rejections by reason: %s", rejected)

    credited = 0
    for p in store.pending_candidates():
        if (store.read_json(p, {}) or {}).get("credit_handle"):
            credited += 1
    log.info(
        "run complete: %d queued, %d pending total (%d with a recovered credit handle)",
        queued,
        len(store.pending_candidates()),
        credited,
    )
    return {"queued": queued, "pending": len(store.pending_candidates())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover candidate posts")
    parser.add_argument("--limit", type=int, default=None, help="max candidates to queue")
    parser.add_argument("--dry-run", action="store_true", help="do not write anything")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        discover(limit=args.limit, dry_run=args.dry_run)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("discovery failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
