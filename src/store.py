"""Filesystem state: config, hashtag budget, dedupe ledger, queue.

Everything is plain JSON committed back to the repo, so the repo itself is the
database. No external services, and the commit history doubles as an audit log.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = ROOT / "config.yml"
STATE_DIR = ROOT / "state"
MEDIA_DIR = ROOT / "media"
QUEUE_PENDING = ROOT / "queue" / "pending"
QUEUE_POSTED = ROOT / "queue" / "posted"

HASHTAG_IDS = STATE_DIR / "hashtag_ids.json"
HASHTAG_QUERIES = STATE_DIR / "hashtag_queries.json"
SEEN_LEDGER = STATE_DIR / "seen.json"
PUBLISH_LOG = STATE_DIR / "published.json"

# Meta: max 30 unique hashtags per rolling 7-day window.
HASHTAG_UNIQUE_CAP = 30
HASHTAG_WINDOW_DAYS = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    # Graph API timestamps look like 2024-01-05T18:30:00+0000
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ensure_dirs() -> None:
    for d in (STATE_DIR, MEDIA_DIR, QUEUE_PENDING, QUEUE_POSTED):
        d.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    tags = cfg.get("hashtags") or []
    if len(tags) > HASHTAG_UNIQUE_CAP:
        raise ValueError(
            f"config.yml lists {len(tags)} hashtags but Meta allows only "
            f"{HASHTAG_UNIQUE_CAP} unique tags per {HASHTAG_WINDOW_DAYS}-day window. "
            "Trim the list."
        )
    return cfg


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("could not read %s (%s); using default", path.name, exc)
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Hashtag id cache + 7-day unique-tag budget
# ---------------------------------------------------------------------------


def cached_hashtag_id(tag: str) -> str | None:
    return read_json(HASHTAG_IDS, {}).get(tag.lower())


def cache_hashtag_id(tag: str, hid: str) -> None:
    ids = read_json(HASHTAG_IDS, {})
    ids[tag.lower()] = hid
    write_json(HASHTAG_IDS, ids)


def _prune_queries(queries: dict[str, str]) -> dict[str, str]:
    cutoff = utcnow() - timedelta(days=HASHTAG_WINDOW_DAYS)
    return {
        tag: ts
        for tag, ts in queries.items()
        if parse_iso(ts) > cutoff
    }


def hashtag_budget() -> dict:
    """Unique tags queried in the trailing 7 days, and headroom remaining."""
    queries = _prune_queries(read_json(HASHTAG_QUERIES, {}))
    return {
        "used": len(queries),
        "cap": HASHTAG_UNIQUE_CAP,
        "remaining": max(0, HASHTAG_UNIQUE_CAP - len(queries)),
        "tags": sorted(queries),
    }


def can_query_hashtag(tag: str) -> bool:
    """True if querying `tag` now stays within the 7-day unique-tag cap.

    A tag already queried inside the window is free -- it is not a new unique
    tag. A brand-new tag needs headroom.
    """
    tag = tag.lower()
    queries = _prune_queries(read_json(HASHTAG_QUERIES, {}))
    if tag in queries:
        return True
    return len(queries) < HASHTAG_UNIQUE_CAP


def record_hashtag_query(tag: str) -> None:
    queries = _prune_queries(read_json(HASHTAG_QUERIES, {}))
    queries[tag.lower()] = iso(utcnow())
    write_json(HASHTAG_QUERIES, queries)


def pick_tags(cfg: dict, count: int) -> list[str]:
    """Round-robin the configured tags, skipping any that would breach budget.

    Ordering is by least-recently-queried so coverage spreads evenly without
    needing randomness (which would break workflow reproducibility).
    """
    tags = [t.lstrip("#").lower() for t in (cfg.get("hashtags") or [])]
    queries = _prune_queries(read_json(HASHTAG_QUERIES, {}))

    def sort_key(tag: str) -> tuple[int, str]:
        ts = queries.get(tag)
        # Never-queried tags sort first (epoch 0).
        return (int(parse_iso(ts).timestamp()) if ts else 0, tag)

    ordered = sorted(tags, key=sort_key)
    chosen: list[str] = []
    for tag in ordered:
        if len(chosen) >= count:
            break
        if can_query_hashtag(tag):
            chosen.append(tag)
        else:
            log.warning(
                "skipping #%s: would exceed the %d-unique-tags/%dd cap",
                tag,
                HASHTAG_UNIQUE_CAP,
                HASHTAG_WINDOW_DAYS,
            )
    return chosen


# ---------------------------------------------------------------------------
# Dedupe ledger
# ---------------------------------------------------------------------------


def seen_ids() -> set[str]:
    return set(read_json(SEEN_LEDGER, {}).get("ids", []))


def mark_seen(media_ids: list[str]) -> None:
    ledger = read_json(SEEN_LEDGER, {"ids": []})
    existing = set(ledger.get("ids", []))
    existing.update(media_ids)
    # Bound growth; oldest-first trim keeps the file from growing forever.
    ordered = sorted(existing)
    if len(ordered) > 20000:
        ordered = ordered[-20000:]
    write_json(SEEN_LEDGER, {"ids": ordered, "updated": iso(utcnow())})


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "item"


def queue_candidate(item: dict) -> Path:
    """Write a candidate into queue/pending. Filename sorts chronologically."""
    ensure_dirs()
    name = f"{iso(utcnow()).replace(':', '')}-{_slug(item.get('source_tag', ''))}-{item['media_id']}.json"
    path = QUEUE_PENDING / name
    write_json(path, item)
    return path


def pending_candidates() -> list[Path]:
    return sorted(QUEUE_PENDING.glob("*.json"))


def archive_candidate(path: Path, result: dict) -> None:
    """Move a candidate out of pending into posted, with the publish result."""
    item = read_json(path, {})
    item["publish_result"] = result
    write_json(QUEUE_POSTED / path.name, item)
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Publish log (local rate guard, independent of the API's own quota)
# ---------------------------------------------------------------------------


def publish_history() -> list[dict]:
    return read_json(PUBLISH_LOG, {"posts": []}).get("posts", [])


def record_publish(entry: dict) -> None:
    log_data = read_json(PUBLISH_LOG, {"posts": []})
    posts = log_data.get("posts", [])
    posts.append(entry)
    # Keep 90 days of history.
    cutoff = utcnow() - timedelta(days=90)
    posts = [p for p in posts if parse_iso(p["at"]) > cutoff]
    write_json(PUBLISH_LOG, {"posts": posts, "updated": iso(utcnow())})


def posts_in_last_24h() -> int:
    cutoff = utcnow() - timedelta(hours=24)
    return sum(1 for p in publish_history() if parse_iso(p["at"]) > cutoff)


def minutes_since_last_post() -> float:
    history = publish_history()
    if not history:
        return float("inf")
    last = max(parse_iso(p["at"]) for p in history)
    return (utcnow() - last).total_seconds() / 60.0
