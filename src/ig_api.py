"""Instagram Graph API client.

Uses the "Instagram API with Facebook Login" flavor, which is the only one that
supports BOTH hashtag search and content publishing. Requires a Facebook Page
linked to an Instagram Professional account.

Endpoints used:
  GET  /ig_hashtag_search                    -> hashtag id (30 unique/7d cap)
  GET  /{hashtag-id}/recent_media            -> media from last 24h
  GET  /{ig-id}/content_publishing_limit     -> posts used in rolling 24h
  POST /{ig-id}/media                        -> create container
  GET  /{container-id}?fields=status_code     -> poll container readiness
  POST /{ig-id}/media_publish                -> publish container
  GET  /oauth/access_token                   -> extend long-lived token
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

GRAPH_VERSION = "v23.0"
BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Meta's documented cap: 100 API-published posts per rolling 24h.
PUBLISH_LIMIT_24H = 100

# Fields available on hashtag recent_media. Note: `username` is NOT available
# here (Meta: "You cannot request the username field on returned media
# objects"), and `user_id` is the *querying* user, not the author.
HASHTAG_MEDIA_FIELDS = (
    "id,media_type,media_url,permalink,caption,timestamp,like_count,comments_count"
)


class IGError(RuntimeError):
    """Graph API returned an error payload."""

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}

    @property
    def code(self) -> int | None:
        return self.payload.get("error", {}).get("code")

    @property
    def subcode(self) -> int | None:
        return self.payload.get("error", {}).get("error_subcode")


class RateLimited(IGError):
    """Hit an API throttle. Caller should back off, not retry immediately."""


# Graph error codes that mean "slow down" rather than "you did it wrong".
_THROTTLE_CODES = {4, 17, 32, 613}


class InstagramClient:
    def __init__(self, ig_user_id: str, access_token: str, timeout: int = 60):
        if not ig_user_id or not access_token:
            raise ValueError("ig_user_id and access_token are both required")
        self.ig_user_id = str(ig_user_id)
        self.token = access_token
        self.timeout = timeout
        self.session = requests.Session()

    # ---------- transport ----------

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        retries: int = 3,
    ) -> dict:
        url = f"{BASE}/{path.lstrip('/')}"
        params = dict(params or {})
        params["access_token"] = self.token

        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.session.request(
                    method, url, params=params, data=data, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_err = exc
                sleep_for = 2**attempt
                log.warning(
                    "network error on %s %s (attempt %d/%d): %s; sleeping %ds",
                    method,
                    path,
                    attempt + 1,
                    retries,
                    exc,
                    sleep_for,
                )
                time.sleep(sleep_for)
                continue

            try:
                payload = resp.json()
            except ValueError:
                payload = {}

            if resp.ok and "error" not in payload:
                return payload

            err = payload.get("error", {})
            code = err.get("code")
            msg = err.get("message", f"HTTP {resp.status_code}")

            if code in _THROTTLE_CODES or resp.status_code == 429:
                # Throttles are not worth hammering. Surface immediately so the
                # workflow can exit clean and let the next cron tick retry.
                raise RateLimited(f"throttled by Graph API: {msg}", payload)

            # 5xx is worth retrying; 4xx generally is not.
            if resp.status_code >= 500 and attempt < retries - 1:
                sleep_for = 2**attempt
                log.warning("server error %s; sleeping %ds", msg, sleep_for)
                time.sleep(sleep_for)
                last_err = IGError(msg, payload)
                continue

            raise IGError(msg, payload)

        raise IGError(f"request failed after {retries} attempts: {last_err}")

    # ---------- discovery ----------

    def hashtag_id(self, tag: str) -> str:
        """Resolve a hashtag name to its (global, static) id.

        Counts against the 30-unique-hashtags-per-7-days budget. Ids never
        change, so cache aggressively -- see state/hashtag_ids.json.
        """
        tag = tag.lstrip("#").strip().lower()
        payload = self._request(
            "GET", "ig_hashtag_search", {"user_id": self.ig_user_id, "q": tag}
        )
        data = payload.get("data") or []
        if not data:
            raise IGError(f"no hashtag id returned for #{tag}")
        return data[0]["id"]

    def hashtag_recent_media(self, hashtag_id: str, limit: int = 50) -> list[dict]:
        """Recent public media for a hashtag.

        Only returns media published within 24h of the query, only public
        photos/videos, never ads. Max 50 per page. No `username` field.
        """
        payload = self._request(
            "GET",
            f"{hashtag_id}/recent_media",
            {
                "user_id": self.ig_user_id,
                "fields": HASHTAG_MEDIA_FIELDS,
                "limit": min(limit, 50),
            },
        )
        return payload.get("data") or []

    def hashtag_top_media(self, hashtag_id: str, limit: int = 50) -> list[dict]:
        """Top media for a hashtag. Not restricted to the last 24h."""
        payload = self._request(
            "GET",
            f"{hashtag_id}/top_media",
            {
                "user_id": self.ig_user_id,
                "fields": HASHTAG_MEDIA_FIELDS,
                "limit": min(limit, 50),
            },
        )
        return payload.get("data") or []

    # ---------- publishing ----------

    def publishing_quota(self) -> dict:
        """Posts consumed in the current rolling 24h window."""
        payload = self._request(
            "GET",
            f"{self.ig_user_id}/content_publishing_limit",
            {"fields": "config,quota_usage"},
        )
        rows = payload.get("data") or [{}]
        row = rows[0]
        used = int(row.get("quota_usage", 0))
        cap = int((row.get("config") or {}).get("quota_total", PUBLISH_LIMIT_24H))
        return {"used": used, "limit": cap, "remaining": max(0, cap - used)}

    def create_image_container(
        self, image_url: str, caption: str, alt_text: str | None = None
    ) -> str:
        """Create an image container. `image_url` must be public HTTPS, JPEG."""
        data: dict[str, Any] = {"image_url": image_url, "caption": caption}
        if alt_text:
            data["alt_text"] = alt_text[:1000]
        payload = self._request("POST", f"{self.ig_user_id}/media", data=data)
        cid = payload.get("id")
        if not cid:
            raise IGError(f"container create returned no id: {payload}")
        return cid

    def container_status(self, container_id: str) -> str:
        """One of: EXPIRED, ERROR, FINISHED, IN_PROGRESS, PUBLISHED."""
        payload = self._request(
            "GET", container_id, {"fields": "status_code,status"}
        )
        return payload.get("status_code", "UNKNOWN")

    def wait_for_container(
        self, container_id: str, timeout: int = 300, interval: int = 5
    ) -> None:
        """Block until the container is FINISHED, or raise."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.container_status(container_id)
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                raise IGError(f"container {container_id} ended in state {status}")
            if status == "PUBLISHED":
                raise IGError(f"container {container_id} was already published")
            time.sleep(interval)
        raise IGError(f"container {container_id} not ready after {timeout}s")

    def publish_container(self, container_id: str) -> str:
        """Publish a FINISHED container. Returns the published media id."""
        payload = self._request(
            "POST", f"{self.ig_user_id}/media_publish", data={"creation_id": container_id}
        )
        mid = payload.get("id")
        if not mid:
            raise IGError(f"publish returned no media id: {payload}")
        return mid

    # ---------- token maintenance ----------

    def extend_token(self, app_id: str, app_secret: str) -> dict:
        """Exchange the current long-lived token for a fresh one (~60 days).

        Long-lived User tokens last about 60 days. Without a periodic refresh
        the bot silently dies, so this runs on a weekly cron.
        """
        payload = self._request(
            "GET",
            "oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": self.token,
            },
        )
        if "access_token" not in payload:
            raise IGError(f"token exchange returned no token: {payload}")
        return payload

    def debug_token(self, app_id: str, app_secret: str) -> dict:
        """Inspect token expiry/scopes. Used by the refresh workflow to report."""
        payload = self._request(
            "GET",
            "debug_token",
            {"input_token": self.token, "access_token": f"{app_id}|{app_secret}"},
        )
        return payload.get("data") or {}
