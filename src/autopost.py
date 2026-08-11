"""Single-run pipeline: discover -> normalize -> publish, in one Actions job.

Why one run instead of two crons
--------------------------------
`media_url` from the API is a SIGNED CDN link carrying `oh=` (HMAC signature)
and `oe=` (hex expiry timestamp). Once `oe` passes, the CDN returns 403. The
old two-cron design discovered on one tick and published on a later one, so it
had to re-host every image to survive that gap.

Publishing in the same run collapses the gap to seconds, which unlocks the
cheap path: hand Instagram the original `media_url` directly and host nothing.

Why hosting is still needed sometimes
-------------------------------------
Meta's publishing rules are strict: "JPEG is the only image format supported",
width 320-1440, aspect ratio 4:5 to 1.91:1. Car photographers routinely post
4000px wides and 9:16 crops. Those fail container creation if hotlinked raw.

So each candidate takes one of two paths:

  PASSTHROUGH -- source is already a legal JPEG. Hotlink `media_url`. Nothing
                 is downloaded, stored, or committed. Zero hosting footprint.

  NORMALIZE   -- source needs conversion. Download, fix it, publish it from a
                 public location, then DELETE it. Meta only requires the file
                 to be reachable "at the time of the attempt", so the file
                 exists publicly for seconds, not forever.

Usage:
    python -m src.autopost [--count N] [--dry-run] [--force] [--no-host]
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import store
from .caption import build_caption, extract_handle
from .discover import passes_filters
from .ig_api import IGError, InstagramClient, RateLimited
from .images import (
    MAX_ASPECT,
    MAX_BYTES,
    MAX_WIDTH,
    MIN_ASPECT,
    MIN_WIDTH,
    ImageError,
    download,
    normalize,
)
from .publish import check_guards, url_is_live

log = logging.getLogger("autopost")


# ---------------------------------------------------------------------------
# Deciding whether a source image can be hotlinked as-is
# ---------------------------------------------------------------------------


def inspect_source(url: str) -> tuple[bool, dict, bytes | None]:
    """Decide if `url` can go straight to Instagram untouched.

    Returns (can_passthrough, details, raw_bytes_if_downloaded).

    We must download to know the real format and dimensions -- Content-Type
    lies often enough that trusting it means failed containers. But downloading
    is cheap and, on the passthrough path, the bytes are simply discarded.
    """
    from PIL import Image
    import io

    try:
        data = download(url)
    except ImageError as exc:
        return False, {"error": str(exc)}, None

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        return False, {"error": f"undecodable: {exc}"}, None

    w, h = img.size
    aspect = w / h if h else 0
    fmt = (img.format or "").upper()

    reasons: list[str] = []
    if fmt != "JPEG":
        reasons.append(f"format is {fmt}, Instagram accepts JPEG only")
    if not (MIN_WIDTH <= w <= MAX_WIDTH):
        reasons.append(f"width {w} outside {MIN_WIDTH}-{MAX_WIDTH}")
    if not (MIN_ASPECT - 0.005 <= aspect <= MAX_ASPECT + 0.005):
        reasons.append(f"aspect {aspect:.3f} outside {MIN_ASPECT:.2f}-{MAX_ASPECT:.2f}")
    if len(data) > MAX_BYTES:
        reasons.append(f"{len(data)} bytes exceeds {MAX_BYTES}")
    if img.mode != "RGB":
        reasons.append(f"mode {img.mode} not RGB")

    details = {
        "format": fmt,
        "width": w,
        "height": h,
        "aspect": round(aspect, 4),
        "bytes": len(data),
        "mode": img.mode,
        "blockers": reasons,
    }
    return (not reasons), details, data


# ---------------------------------------------------------------------------
# Ephemeral public hosting, for the NORMALIZE path only
# ---------------------------------------------------------------------------


class EphemeralHost:
    """Publishes one file to a public git branch, then removes it.

    Meta requires the image to be reachable "at the time of the attempt" -- not
    permanently. So we push, publish, and delete. The file is public for
    roughly a minute rather than living in history forever.

    Target is a dedicated public repo (MEDIA_REPO, e.g. "Datto91/ig-car-media")
    so the code repo itself can stay private. Falls back to the current repo
    when MEDIA_REPO is unset.
    """

    def __init__(self) -> None:
        self.repo = os.environ.get("MEDIA_REPO") or os.environ.get("GITHUB_REPOSITORY")
        self.branch = os.environ.get("MEDIA_BRANCH", "main")
        self.token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
        self.workdir: Path | None = None
        self.pushed: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.repo and self.token)

    def _run(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(
            args, cwd=cwd or self.workdir, check=True, capture_output=True, text=True
        )

    def _clone(self) -> None:
        if self.workdir:
            return
        self.workdir = Path(tempfile.mkdtemp(prefix="ig-media-"))
        url = f"https://x-access-token:{self.token}@github.com/{self.repo}.git"
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", self.branch, url, str(self.workdir)],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            # Never leak the token-bearing URL into logs.
            raise RuntimeError(
                f"could not clone media repo {self.repo} (branch {self.branch}); "
                f"git exited {exc.returncode}"
            ) from None
        self._run("git", "config", "user.name", "ig-car-autopost[bot]")
        self._run("git", "config", "user.email", "actions@github.com")

    def put(self, local: Path) -> str:
        """Push `local` and return its public raw URL."""
        self._clone()
        assert self.workdir
        target = self.workdir / local.name
        target.write_bytes(local.read_bytes())
        self._run("git", "add", local.name)
        self._run("git", "commit", "-m", f"tmp: {local.name}")
        self._run("git", "push", "origin", f"HEAD:{self.branch}")
        self.pushed.append(local.name)
        return f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{local.name}"

    def cleanup(self) -> None:
        """Delete every file we pushed. Best-effort; never raises."""
        if not self.workdir or not self.pushed:
            return
        try:
            for name in self.pushed:
                path = self.workdir / name
                if path.exists():
                    self._run("git", "rm", "--quiet", name)
            self._run("git", "commit", "-m", "tmp: remove published media")
            self._run("git", "push", "origin", f"HEAD:{self.branch}")
            log.info("removed %d temporary image(s) from %s", len(self.pushed), self.repo)
        except Exception as exc:
            log.warning(
                "could not clean up temporary media (%s); %d file(s) may remain in %s",
                exc,
                len(self.pushed),
                self.repo,
            )
        finally:
            import shutil

            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None
            self.pushed = []


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def gather_candidates(client: InstagramClient, cfg: dict, want: int) -> list[dict]:
    """Query hashtags until we have `want` fresh, filter-passing candidates."""
    seen = store.seen_ids()
    tags = store.pick_tags(cfg, int(cfg.get("tags_per_run", 5)))
    if not tags:
        log.error("hashtag budget exhausted (30 unique tags / 7 days); nothing to query")
        return []

    use_top = bool(cfg.get("use_top_media", True))
    out: list[dict] = []

    for tag in tags:
        if len(out) >= want:
            break

        hid = store.cached_hashtag_id(tag)
        try:
            if not hid:
                hid = client.hashtag_id(tag)
                store.cache_hashtag_id(tag, hid)
            store.record_hashtag_query(tag)
            media_list = (
                client.hashtag_top_media(hid) if use_top else client.hashtag_recent_media(hid)
            )
        except RateLimited as exc:
            log.error("throttled on #%s: %s -- stopping discovery", tag, exc)
            break
        except IGError as exc:
            log.error("#%s failed: %s", tag, exc)
            continue

        log.info("#%s -> %d media objects", tag, len(media_list))

        for media in media_list:
            if len(out) >= want:
                break
            mid = media.get("id")
            if not mid or mid in seen:
                continue
            ok, reason = passes_filters(media, cfg)
            if not ok:
                log.debug("skip %s: %s", mid, reason)
                continue
            caption_src = media.get("caption") or ""
            cand = {
                "media_id": mid,
                "source_tag": tag,
                "permalink": media.get("permalink"),
                "source_media_url": media.get("media_url"),
                "source_caption": caption_src,
                "credit_handle": extract_handle(caption_src),
                "like_count": media.get("like_count"),
                "discovered_at": store.iso(store.utcnow()),
            }
            cand["caption"] = build_caption(cfg, cand)
            out.append(cand)

    return out


def run(
    count: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    allow_host: bool = True,
) -> dict:
    cfg = store.load_config()
    store.ensure_dirs()

    ig_user_id = os.environ.get("IG_USER_ID")
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not ig_user_id or not token:
        raise SystemExit("IG_USER_ID and IG_ACCESS_TOKEN must be set")

    client = InstagramClient(ig_user_id, token)

    ok, reason = check_guards(cfg, client, force)
    if not ok:
        log.warning("not posting: %s", reason)
        return {"published": 0, "reason": reason}

    want = count if count is not None else int((cfg.get("publish") or {}).get("posts_per_run", 1))
    # Over-fetch: some candidates will fail image inspection.
    candidates = gather_candidates(client, cfg, want * 4)
    if not candidates:
        return {"published": 0, "reason": "no candidates found"}

    log.info("%d candidate(s) gathered, need %d", len(candidates), want)

    host = EphemeralHost()
    tmpdir = Path(tempfile.mkdtemp(prefix="ig-norm-"))
    published = 0
    stats = {"passthrough": 0, "normalized": 0, "skipped": 0}
    attempted: list[str] = []

    try:
        for cand in candidates:
            if published >= want:
                break

            mid = cand["media_id"]
            src = cand.get("source_media_url")
            if not src:
                continue

            attempted.append(mid)
            can_pass, details, data = inspect_source(src)

            if details.get("error"):
                log.warning("%s: %s", mid, details["error"])
                stats["skipped"] += 1
                continue

            if can_pass:
                image_url = src
                mode = "passthrough"
                log.info(
                    "%s PASSTHROUGH (%dx%d JPEG, %.0fKB) -- hotlinking, nothing stored",
                    mid,
                    details["width"],
                    details["height"],
                    details["bytes"] / 1024,
                )
            else:
                log.info("%s needs normalizing: %s", mid, "; ".join(details["blockers"]))
                if not allow_host:
                    log.info("%s skipped (--no-host and source is not publishable as-is)", mid)
                    stats["skipped"] += 1
                    continue
                if not host.available:
                    log.warning(
                        "%s needs normalizing but no public host configured "
                        "(set MEDIA_REPO + GH_PAT); skipping",
                        mid,
                    )
                    stats["skipped"] += 1
                    continue
                local = tmpdir / f"{mid}.jpg"
                try:
                    meta = normalize(data, local)
                except ImageError as exc:
                    log.warning("%s could not be normalized: %s", mid, exc)
                    stats["skipped"] += 1
                    continue
                log.info(
                    "%s normalized -> %dx%d aspect %s (%s)",
                    mid,
                    meta["width"],
                    meta["height"],
                    meta["aspect"],
                    "; ".join(meta["adjustments"]) or "no changes",
                )
                if dry_run:
                    log.info("[dry-run] would host and publish %s", mid)
                    stats["normalized"] += 1
                    published += 1
                    continue
                image_url = host.put(local)
                mode = "normalized"
                log.info("%s hosted transiently at %s", mid, image_url)

            handle = cand.get("credit_handle")
            log.info(
                "publishing %s from #%s (credit=%s)",
                mid,
                cand["source_tag"],
                f"@{handle}" if handle else "none recovered",
            )
            log.info("caption:\n%s", cand["caption"])

            if dry_run:
                log.info("[dry-run] stopping before container creation")
                stats[mode] += 1
                published += 1
                continue

            if mode == "normalized" and not url_is_live(image_url):
                log.error("%s: hosted URL not reachable yet; skipping", mid)
                stats["skipped"] += 1
                continue

            alt = f"Car photo{f' by @{handle}' if handle else ''}."
            try:
                cid = client.create_image_container(image_url, cand["caption"], alt_text=alt)
                client.wait_for_container(
                    cid, timeout=int((cfg.get("publish") or {}).get("container_timeout", 300))
                )
                pid = client.publish_container(cid)
            except RateLimited as exc:
                log.error("throttled while publishing: %s -- stopping", exc)
                break
            except IGError as exc:
                log.error("%s failed to publish: %s", mid, exc)
                stats["skipped"] += 1
                continue

            log.info("%s published as %s (%s)", mid, pid, mode)
            record = {
                "at": store.iso(store.utcnow()),
                "media_id": mid,
                "published_id": pid,
                "container_id": cid,
                "mode": mode,
                "source_permalink": cand.get("permalink"),
                "credit_handle": handle,
                "source_tag": cand["source_tag"],
                "image": details,
            }
            store.record_publish(record)
            store.write_json(store.QUEUE_POSTED / f"{mid}.json", {**cand, "publish_result": record})
            stats[mode] += 1
            published += 1

    finally:
        host.cleanup()
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)
        # Mark everything we touched so it is never reconsidered.
        if attempted and not dry_run:
            store.mark_seen(attempted)

    log.info(
        "done: %d published (%d hotlinked, %d normalized, %d skipped)",
        published,
        stats["passthrough"],
        stats["normalized"],
        stats["skipped"],
    )
    return {"published": published, **stats}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Discover and publish in a single run")
    p.add_argument("--count", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore local cadence guards")
    p.add_argument(
        "--no-host",
        action="store_true",
        help="only publish images already legal for Instagram; never host anything",
    )
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        run(
            count=args.count,
            dry_run=args.dry_run,
            force=args.force,
            allow_host=not args.no_host,
        )
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("autopost failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
