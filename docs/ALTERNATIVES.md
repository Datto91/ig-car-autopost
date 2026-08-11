# Fallback: Business Discovery sourcing

Read this if **Instagram Public Content Access** gets denied in App Review, or
if business verification stalls. It is also the path that gives you real
attribution.

## Why it might be needed

Hashtag Search requires the Public Content Access feature, which requires
App Review **plus business verification** (legal business documents). Approval is
not guaranteed, and Meta's documented allowed usages for that feature are about
monitoring your own brand and campaigns — not sourcing other creators' photos.

`business_discovery` requires **neither**. From the
[reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery),
its only requirements are the permissions `instagram_basic`,
`instagram_manage_insights`, and `pages_read_engagement`.

## What changes

| | Hashtag Search | Business Discovery |
|---|---|---|
| Business verification | **Required** | Not required |
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
