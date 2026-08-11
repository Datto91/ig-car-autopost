"""Weekly token maintenance.

Long-lived Facebook User access tokens last ~60 days. If nothing refreshes
them, the bot dies quietly two months after you set it up and the only symptom
is workflows failing with OAuthException. This script:

  1. Inspects the current token and reports days remaining.
  2. Exchanges it for a fresh long-lived token.
  3. Pushes the new token back into the repo's Actions secret via `gh`, so the
     refresh is genuinely automatic rather than a reminder to do it by hand.

Step 3 needs a GitHub PAT with `secrets:write` on the repo, exposed as
GH_PAT. Without it the script still reports expiry and prints instructions,
but cannot self-heal.

Usage:
    python -m src.refresh_token [--check-only]
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from .ig_api import IGError, InstagramClient

log = logging.getLogger("refresh-token")

# Warn loudly when the token has less than this many days left.
WARN_THRESHOLD_DAYS = 14


def describe_token(client: InstagramClient, app_id: str, app_secret: str) -> dict:
    try:
        info = client.debug_token(app_id, app_secret)
    except IGError as exc:
        log.warning("could not introspect token: %s", exc)
        return {}

    expires_at = info.get("expires_at")
    data_expires = info.get("data_access_expires_at")

    def _fmt(ts: int | None) -> str:
        if not ts:
            return "never (or not reported)"
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        days = (dt - datetime.now(timezone.utc)).days
        return f"{dt:%Y-%m-%d %H:%M UTC} ({days}d)"

    log.info("token type:        %s", info.get("type"))
    log.info("app id:            %s", info.get("app_id"))
    log.info("valid:             %s", info.get("is_valid"))
    log.info("expires:           %s", _fmt(expires_at))
    log.info("data access ends:  %s", _fmt(data_expires))
    log.info("scopes:            %s", ", ".join(info.get("scopes") or []))

    if expires_at:
        days_left = (
            datetime.fromtimestamp(expires_at, tz=timezone.utc)
            - datetime.now(timezone.utc)
        ).days
        if days_left < WARN_THRESHOLD_DAYS:
            log.warning(
                "token expires in %dd -- refresh must succeed or posting stops",
                days_left,
            )
    return info


def update_repo_secret(name: str, value: str) -> bool:
    """Write the refreshed token back into GitHub Actions secrets via gh CLI."""
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not pat or not repo:
        log.warning(
            "GH_PAT and/or GITHUB_REPOSITORY not set -- cannot self-update the "
            "secret. Set the %s secret manually to the value printed by "
            "`--check-only` runs, or add a GH_PAT with secrets:write.",
            name,
        )
        return False

    env = dict(os.environ, GH_TOKEN=pat)
    try:
        subprocess.run(
            ["gh", "secret", "set", name, "--repo", repo, "--body", value],
            check=True,
            env=env,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        log.error("gh CLI not found on PATH; cannot update secret %s", name)
        return False
    except subprocess.CalledProcessError as exc:
        # Never echo the token itself into logs.
        log.error("gh secret set failed (exit %s): %s", exc.returncode, exc.stderr)
        return False

    log.info("updated Actions secret %s on %s", name, repo)
    return True


def run(check_only: bool = False) -> int:
    ig_user_id = os.environ.get("IG_USER_ID")
    token = os.environ.get("IG_ACCESS_TOKEN")
    app_id = os.environ.get("FB_APP_ID")
    app_secret = os.environ.get("FB_APP_SECRET")

    missing = [
        n
        for n, v in (
            ("IG_USER_ID", ig_user_id),
            ("IG_ACCESS_TOKEN", token),
            ("FB_APP_ID", app_id),
            ("FB_APP_SECRET", app_secret),
        )
        if not v
    ]
    if missing:
        raise SystemExit(f"missing required environment variables: {', '.join(missing)}")

    client = InstagramClient(ig_user_id, token)

    log.info("--- current token ---")
    describe_token(client, app_id, app_secret)

    if check_only:
        return 0

    log.info("--- exchanging for a fresh long-lived token ---")
    try:
        fresh = client.extend_token(app_id, app_secret)
    except IGError as exc:
        log.error("token exchange failed: %s", exc)
        log.error(
            "If this says the token is invalid or expired, the automatic chain "
            "is broken and you must re-run the manual token flow in "
            "docs/SETUP.md step 6."
        )
        return 1

    new_token = fresh["access_token"]
    expires_in = fresh.get("expires_in")
    if expires_in:
        log.info("new token valid for ~%d days", int(expires_in) // 86400)

    if new_token == token:
        log.info("Graph returned the same token; nothing to update.")
        return 0

    if update_repo_secret("IG_ACCESS_TOKEN", new_token):
        log.info("token rotation complete")
        return 0

    # Could not persist it. Fail loudly -- a silent success here would mean the
    # bot dies at day 60 with no warning.
    log.error(
        "obtained a fresh token but could not persist it to Actions secrets. "
        "Posting will stop when the current token expires."
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and refresh the IG access token")
    parser.add_argument(
        "--check-only", action="store_true", help="report expiry without rotating"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    try:
        return run(check_only=args.check_only)
    except SystemExit:
        raise
    except Exception as exc:
        log.exception("refresh failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
