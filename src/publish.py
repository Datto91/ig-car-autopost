"""Publish: pop an approved candidate off the queue and post it.

Runs on a cron. Every run is guarded so a bug or a runaway loop cannot spray
the account:

  * kill switch        -- config.yml `enabled: false` stops everything
  * local rate guard   -- state/published.json, daily_cap + min_gap_minutes
  * server rate guard  -- GET /content_publishing_limit before every publish
  * URL preflight      -- confirm the re-hosted JPEG is actually reachable
                          before asking Meta to cURL it

Usage:
    python -m src.publish [--count N] [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import requests

from . import store
from .ig_api import IGError, InstagramClient, RateLimited

log = logging.getLogger("publish")


def media_base_url() -> str:
    """Public base URL for the re-hosted images.

    Instagram's POST /media does not accept uploaded bytes -- it cURLs a URL we
    give it ("We will cURL your image using the passed in URL so it must be on
    a public server"). So the images must be served from somewhere public and
    unauthenticated.

    Resolution order:
      1. MEDIA_BASE_URL env var  -- explicit override, e.g. a separate public
         media repo or a CDN.
      2. Derived from GITHUB_REPOSITORY, which Actions always sets.
      3. Hardcoded fallback for local runs.

    NOTE: raw.githubusercontent.com only serves PUBLIC repositories. A private
    repo returns 404 to Instagram's fetcher. See docs/SETUP.md "Public media"
    for the two supported layouts.
    """
    explicit = os.environ.get("MEDIA_BASE_URL")
    if explicit:
        return explicit.rstrip("/")

    repo = os.environ.get("GITHUB_REPOSITORY")  # "owner/name"
    branch = os.environ.get("MEDIA_BRANCH", "main")
    if repo:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/media"

    return "https://raw.githubusercontent.com/Datto91/ig-car-autopost/main/media"


def url_is_live(url: str, attempts: int = 3) -> bool:
    """Confirm the image URL serves before handing it to Meta.

    raw.githubusercontent.com can lag a push by a few seconds, and a container
    created against a 404 fails in a way that burns a queue item.
    """
    import time

    for attempt in range(attempts):
        try:
            resp = requests.head(url, timeout=20, allow_redirects=True)
            if resp.ok:
                return True
            # raw.githubusercontent sometimes 404s a HEAD but serves a GET.
            resp = requests.get(url, timeout=20, stream=True)
            if resp.ok:
                return True
            log.warning("preflight for %s returned HTTP %s", url, resp.status_code)
        except requests.RequestException as exc:
            log.warning("preflight for %s failed: %s", url, exc)
        if attempt < attempts - 1:
            time.sleep(5 * (attempt + 1))
    return False


def check_guards(cfg: dict, client: InstagramClient, force: bool) -> tuple[bool, str]:
    """Every reason we might refuse to publish right now."""
    if not cfg.get("enabled", False):
        return False, "kill switch: config.yml has enabled: false"

    pub = cfg.get("publish") or {}

    if not force:
        daily_cap = int(pub.get("daily_cap", 6))
        posted_24h = store.posts_in_last_24h()
        if posted_24h >= daily_cap:
            return False, f"local daily cap reached ({posted_24h}/{daily_cap} in 24h)"

        gap_required = float(pub.get("min_gap_minutes", 90))
        gap_actual = store.minutes_since_last_post()
        if gap_actual < gap_required:
            return (
                False,
                f"only {gap_actual:.0f}m since last post, need {gap_required:.0f}m",
            )

    # Server-side truth. Cheap call, and it is the limit that actually matters.
    try:
        quota = client.publishing_quota()
    except IGError as exc:
        log.warning("could not read publishing quota (%s); relying on local guards", exc)
        return True, ""

    log.info(
        "server publishing quota: %d/%d used, %d remaining",
        quota["used"],
        quota["limit"],
        quota["remaining"],
    )
    if quota["remaining"] <= 0:
        return False, f"server quota exhausted ({quota['used']}/{quota['limit']} in 24h)"

    return True, ""


def publish_one(
    client: InstagramClient, cfg: dict, path: Path, dry_run: bool
) -> dict | None:
    candidate = store.read_json(path, {})
    if not candidate:
        log.warning("queue item %s is empty or unreadable; removing", path.name)
        path.unlink(missing_ok=True)
        return None

    mid = candidate.get("media_id", "?")
    image_meta = candidate.get("image") or {}
    filename = image_meta.get("path")
    if not filename:
        log.warning("queue item %s has no re-hosted image; dropping", path.name)
        path.unlink(missing_ok=True)
        return None

    local_path = store.MEDIA_DIR / filename
    if not local_path.exists():
        log.warning("image %s missing on disk for %s; dropping", filename, path.name)
        path.unlink(missing_ok=True)
        return None

    image_url = f"{media_base_url()}/{filename}"
    caption = candidate.get("caption") or ""
    handle = candidate.get("credit_handle")

    log.info(
        "publishing %s from #%s (credit=%s)",
        mid,
        candidate.get("source_tag"),
        f"@{handle}" if handle else "none recovered",
    )
    log.info("image_url: %s", image_url)
    log.info("caption:\n%s", caption)

    if dry_run:
        log.info("[dry-run] stopping before container creation")
        return None

    if not url_is_live(image_url):
        log.error(
            "image_url is not publicly reachable: %s -- leaving %s queued. "
            "If the repo is private, raw.githubusercontent.com will 404 for "
            "Instagram; see docs/SETUP.md.",
            image_url,
            path.name,
        )
        return None

    alt = f"Car photo{f' by @{handle}' if handle else ''}."

    container_id = client.create_image_container(image_url, caption, alt_text=alt)
    log.info("container created: %s", container_id)

    timeout = int((cfg.get("publish") or {}).get("container_timeout", 300))
    client.wait_for_container(container_id, timeout=timeout)
    log.info("container %s is FINISHED", container_id)

    published_id = client.publish_container(container_id)
    log.info("published media id: %s", published_id)

    result = {
        "at": store.iso(store.utcnow()),
        "media_id": mid,
        "published_id": published_id,
        "container_id": container_id,
        "image_url": image_url,
        "source_permalink": candidate.get("permalink"),
        "credit_handle": handle,
        "source_tag": candidate.get("source_tag"),
    }
    store.record_publish(result)
    store.archive_candidate(path, result)
    return result


def run(count: int | None = None, dry_run: bool = False, force: bool = False) -> dict:
    cfg = store.load_config()
    store.ensure_dirs()

    ig_user_id = os.environ.get("IG_USER_ID")
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not ig_user_id or not token:
        raise SystemExit("IG_USER_ID and IG_ACCESS_TOKEN must be set in the environment")

    client = InstagramClient(ig_user_id, token)

    ok, reason = check_guards(cfg, client, force)
    if not ok:
        log.warning("not publishing: %s", reason)
        return {"published": 0, "reason": reason}

    pending = store.pending_candidates()
    if not pending:
        log.info("queue is empty; nothing to publish")
        return {"published": 0, "reason": "empty queue"}

    want = count if count is not None else int((cfg.get("publish") or {}).get("posts_per_run", 1))
    published = 0

    for path in pending:
        if published >= want:
            break
        try:
            result = publish_one(client, cfg, path, dry_run)
            if result:
                published += 1
        except RateLimited as exc:
            log.error("throttled while publishing: %s -- stopping run", exc)
            break
        except IGError as exc:
            log.error("publish failed for %s: %s", path.name, exc)
            # Container/URL errors are usually specific to this item. Move it
            # aside so a single poison item cannot block the queue forever.
            store.archive_candidate(
                path, {"at": store.iso(store.utcnow()), "error": str(exc)}
            )
            continue

    log.info("run complete: %d published, %d still pending", published, len(store.pending_candidates()))
    return {"published": published, "pending": len(store.pending_candidates())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish queued posts to Instagram")
    parser.add_argument("--count", type=int, default=None, help="how many to publish")
    parser.add_argument("--dry-run", action="store_true", help="stop before posting")
    parser.add_argument(
        "--force", action="store_true", help="ignore local cadence guards (not server quota)"
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        run(count=args.count, dry_run=args.dry_run, force=args.force)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("publish failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
