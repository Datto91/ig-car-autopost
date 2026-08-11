"""Preflight: prove the credentials work before trusting the cron.

Read-only. Publishes nothing, queues nothing, writes nothing. Every check is a
GET, so a failure here costs you nothing but information.

Order matters -- each check depends on the one above it, so the first failure is
the actual root cause rather than a downstream symptom:

  1. Token shape     -- EAA (Facebook Login) vs IGAA (Instagram Login, wrong)
  2. Token validity  -- is it live, whose app, when does it expire, what scopes
  3. Account access  -- can we read the IG user this token is paired with
  4. Hashtag search  -- the Standard Access feature the whole design rests on
  5. Media fetch     -- do we get usable image URLs and captions back
  6. Publish quota   -- is the publishing endpoint reachable and how much is left

Usage:
    python -m src.preflight
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from .ig_api import IGError, InstagramClient, RateLimited

log = logging.getLogger("preflight")

# Anything the user should not see echoed into a public Actions log.
_SECRET_ENV = ("IG_ACCESS_TOKEN", "FB_APP_SECRET", "GH_PAT")


def _redact(text: str) -> str:
    """Strip secret values out of a string before logging it."""
    for name in _SECRET_ENV:
        val = os.environ.get(name)
        if val and len(val) > 8:
            text = text.replace(val, f"<{name} redacted>")
    return text


def _ok(msg: str) -> None:
    log.info("  PASS  %s", msg)


def _fail(msg: str) -> None:
    log.error("  FAIL  %s", _redact(msg))


def _warn(msg: str) -> None:
    log.warning("  WARN  %s", msg)


def run() -> int:
    ig_user_id = os.environ.get("IG_USER_ID")
    token = os.environ.get("IG_ACCESS_TOKEN")
    app_id = os.environ.get("FB_APP_ID")
    app_secret = os.environ.get("FB_APP_SECRET")

    failures = 0

    # --- 1. presence + token shape -------------------------------------------
    log.info("[1/6] credentials present and correctly shaped")
    if not ig_user_id:
        _fail("IG_USER_ID is not set")
        return 1
    _ok(f"IG_USER_ID = {ig_user_id}")

    if not token:
        _fail(
            "IG_ACCESS_TOKEN is not set. Note the workflows also accept a secret "
            "named IG_TOKEN_ACCESS, so check the spelling of whichever you created."
        )
        return 1

    if token.startswith("IGAA"):
        _fail(
            "this is an Instagram Login token (starts with IGAA). Hashtag Search "
            "only exists on 'Instagram API with Facebook Login', whose tokens "
            "start with EAA. See docs/SETUP.md step 2 -- the app needs the "
            "Facebook Login flavor."
        )
        return 1
    if not token.startswith("EAA"):
        _warn(
            f"token starts with {token[:4]!r}; a Facebook Login user token normally "
            "starts with EAA. Continuing, but this is the usual cause of the "
            "auth failures below."
        )
    else:
        _ok("token looks like a Facebook Login token (EAA...)")

    client = InstagramClient(ig_user_id, token)

    # --- 2. token validity ---------------------------------------------------
    log.info("[2/6] token is valid (GET /debug_token)")
    if app_id and app_secret:
        try:
            info = client.debug_token(app_id, app_secret)
            if not info.get("is_valid"):
                _fail(f"token reports is_valid=false: {info.get('error', {})}")
                failures += 1
            else:
                _ok(f"valid, type={info.get('type')}, app_id={info.get('app_id')}")

            expires_at = info.get("expires_at")
            if expires_at:
                dt = datetime.fromtimestamp(expires_at, tz=timezone.utc)
                days = (dt - datetime.now(timezone.utc)).days
                if days < 7:
                    _warn(
                        f"expires in {days}d ({dt:%Y-%m-%d}) -- exchange it for a "
                        "long-lived token (setup step 6) or the bot dies soon"
                    )
                else:
                    _ok(f"expires {dt:%Y-%m-%d} ({days}d away)")
            else:
                _ok("no expiry reported (long-lived or never-expiring)")

            scopes = info.get("scopes") or []
            _ok(f"scopes: {', '.join(scopes) if scopes else '(none reported)'}")
            for needed in ("instagram_basic", "pages_read_engagement"):
                if scopes and needed not in scopes:
                    _warn(f"scope {needed!r} not present; some calls may fail")
        except IGError as exc:
            _fail(f"could not introspect token: {exc}")
            failures += 1
    else:
        _warn("FB_APP_ID / FB_APP_SECRET not set -- skipping token introspection")

    # --- 3. account access ---------------------------------------------------
    log.info("[3/6] can read the Instagram account")
    try:
        me = client._request(
            "GET", ig_user_id, {"fields": "id,username,name,followers_count,media_count"}
        )
        _ok(
            f"@{me.get('username')} ({me.get('name') or 'no name'}) -- "
            f"{me.get('followers_count', '?')} followers, {me.get('media_count', '?')} posts"
        )
        if str(me.get("id")) != str(ig_user_id):
            _warn(f"returned id {me.get('id')} differs from IG_USER_ID {ig_user_id}")
    except IGError as exc:
        _fail(
            f"cannot read the account: {exc}\n"
            "        Usual causes: the IG account is not a role user on the app "
            "(App Dashboard -> App Roles -> Roles, and the Instagram Tester "
            "invite must be ACCEPTED from the Instagram side under Settings -> "
            "Apps and Websites), or the token belongs to a different app."
        )
        return 1 + failures

    # --- 4. hashtag search (the Standard Access feature) ---------------------
    log.info("[4/6] hashtag search works (Instagram Public Content Access)")
    probe = "jdm"
    hashtag_id = None
    try:
        hashtag_id = client.hashtag_id(probe)
        _ok(f"#{probe} resolved to id {hashtag_id}")
    except RateLimited as exc:
        _warn(f"throttled resolving #{probe}: {exc} -- retry later")
    except IGError as exc:
        _fail(
            f"hashtag search failed: {exc}\n"
            "        This is the feature the whole design depends on. Check that "
            "the app type is Business and that Instagram Public Content Access "
            "is listed under App Review -> Permissions and Features (Standard "
            "Access is automatic, but the feature still has to be added)."
        )
        failures += 1

    # --- 5. media fetch ------------------------------------------------------
    log.info("[5/6] hashtag media returns usable candidates")
    if hashtag_id:
        try:
            media = client.hashtag_top_media(hashtag_id, limit=10)
            if not media:
                _warn("hashtag returned zero media -- unusual; try another tag")
            else:
                images = [m for m in media if m.get("media_type") == "IMAGE"]
                with_url = [m for m in images if m.get("media_url")]
                with_caption = [m for m in media if m.get("caption")]
                _ok(
                    f"{len(media)} media, {len(images)} images, "
                    f"{len(with_url)} with a fetchable media_url, "
                    f"{len(with_caption)} with a caption"
                )
                if not with_url:
                    _warn(
                        "no media_url on any item -- copyright-flagged media and "
                        "owners who disabled downloads omit that field"
                    )
                sample = (with_url or images or media)[0]
                _ok(f"sample permalink: {sample.get('permalink')}")
        except IGError as exc:
            _fail(f"could not fetch hashtag media: {exc}")
            failures += 1
    else:
        _warn("skipped -- no hashtag id from step 4")

    # --- 6. publishing endpoint ---------------------------------------------
    log.info("[6/6] publishing endpoint reachable")
    try:
        quota = client.publishing_quota()
        _ok(
            f"{quota['used']}/{quota['limit']} posts used in the rolling 24h "
            f"({quota['remaining']} remaining)"
        )
    except IGError as exc:
        _fail(
            f"publishing quota unreadable: {exc}\n"
            "        Usually a missing instagram_content_publish permission, or "
            "the Page needs Page Publishing Authorization."
        )
        failures += 1

    log.info("")
    if failures:
        log.error("PREFLIGHT FAILED -- %d check(s) failed. Nothing was published.", failures)
        return 1

    log.info("PREFLIGHT PASSED -- credentials are good. Nothing was published.")
    log.info("Next: run the discover workflow with dry_run=true to see real candidates.")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return run()
    except SystemExit:
        raise
    except Exception as exc:
        log.error("preflight crashed: %s", _redact(str(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
