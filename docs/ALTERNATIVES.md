# Alternative: Business Discovery sourcing

**This is not a fallback for an App Review problem** — hashtag search works at
Standard Access without review (see
[SETUP.md Step 4](SETUP.md#step-4--access-levels-why-you-dont-need-app-review)).
Read this for the one thing hashtag search genuinely cannot do: **return the
author's username.**

## Why you might switch

Hashtag Search never returns the creator's handle — Meta's docs are explicit
(*"You cannot request the username field on returned media objects"*), so credit
is best-effort, recovered only when the creator typed a handle into their own
caption.

`business_discovery` returns `username` at the account level, so every post it
finds has a known author. Both endpoints work at Standard Access; the
[reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery)
lists its requirements as `instagram_basic`, `instagram_manage_insights`, and
`pages_read_engagement`.

## What changes

| | Hashtag Search | Business Discovery |
|---|---|---|
| App Review needed | No (Standard Access) | No (Standard Access) |
| Author username | **Not available** | **Returned** |
| Time window | Last 24h (`recent_media`) | No window |
| Unique-tag cap | 30 per 7 days | N/A |
| Discovery model | By tag | By account you name |
| Covers personal accounts | No | No (Professional only) |

The tradeoff is that you supply the account list instead of discovering by tag.
For an established car page that is usually an improvement — you already know
which accounts post the good stuff, and quality control comes free.

## The call

```
GET /{ig-user-id}?fields=business_discovery.username(TARGET){
      username,
      followers_count,
      media{id,media_type,media_url,permalink,caption,timestamp,like_count,comments_count}
    }
```

`username` comes back at the account level, so every media object it returns has
a known author. That makes the credit line reliable instead of best-effort.

## Wiring it in

`src/discover.py` is the only file that needs to change. Add a sourcing function
alongside the hashtag path:

```python
def discover_via_business_discovery(client, cfg):
    """Poll a curated account list. Author username is always known."""
    for username in cfg["accounts"]:
        fields = (
            f"business_discovery.username({username})"
            "{username,media{id,media_type,media_url,permalink,"
            "caption,timestamp,like_count,comments_count}}"
        )
        payload = client._request("GET", client.ig_user_id, {"fields": fields})
        bd = payload.get("business_discovery", {})
        author = bd.get("username")
        for media in (bd.get("media") or {}).get("data", []):
            yield author, media
```

Then in the candidate dict, set `credit_handle` from `author` directly instead of
calling `extract_handle()`. Everything downstream — filtering, image
normalization, queueing, publishing, rate guards — works unchanged.

Add to `config.yml`:

```yaml
accounts:
  - carsofinstagram
  - carlifestyle
  # ... 50-100 handles. Professional accounts only; personal ones return nothing.
```

## Third option: submissions only

The `mentions` / `tags` edges return media where your account was tagged or
@mentioned. Author is always known, permissions are the lightest of all three,
and the permission story is cleanest — people are actively submitting their car
to your page. Lower volume until the page has reach, but it is the only sourcing
model where consent is implied by the submission itself.
