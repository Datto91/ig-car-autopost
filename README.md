# ig-car-autopost

Fully automatic Instagram posting for a car page, hosted entirely on GitHub
Actions. Sources content via Instagram Hashtag Search, re-hosts and normalizes
the images, and publishes on a schedule with no human in the loop.

```
discover (cron 4x/day)          publish (cron 3x/day)         refresh-token (weekly)
  hashtag search                  guards: kill switch,           debug_token
  filter candidates               daily cap, min gap,            fb_exchange_token
  download + normalize JPEG       server quota                   write back to
  commit to media/ + queue/       POST /media -> poll ->         Actions secret
                                  media_publish
```

## Why it's built this way

Three constraints from Meta's docs drive the whole design:

1. **`POST /media` does not accept file uploads.** Meta cURLs a URL you provide,
   so images must be publicly hosted. The repo doubles as the image host.
2. **Source `media_url` values expire.** Discovery and publishing run on
   different cron ticks, so images are downloaded and re-hosted at discovery
   time rather than hotlinked.
3. **Hashtag search returns no author username.** See below.

## Attribution, honestly

The IG Hashtag Search API does not return the author's handle — Meta's docs state
*"You cannot request the username field on returned media objects"*, and the
`user_id` it does return is the querying user, not the author. The oEmbed
endpoint no longer returns `author_name` and its terms forbid persisting metadata.

So [src/caption.py](src/caption.py) recovers a handle **only** when the creator
typed one into their own caption (self-tags, "shot by @x", 📸 @x). That works on a
meaningful fraction of posts. When nothing is found, the credit line is omitted
rather than invented.

If reliable attribution matters, switch sourcing to `business_discovery` —
see [docs/ALTERNATIVES.md](docs/ALTERNATIVES.md). It returns usernames and needs
no business verification.

## Setup

[docs/SETUP.md](docs/SETUP.md). Start with **Step 4** — hashtag search requires
App Review *plus* Meta business verification, which is the step most likely to
block this project and the one entirely outside your control.

## Layout

| Path | Purpose |
|---|---|
| [src/ig_api.py](src/ig_api.py) | Graph API client, throttle handling, token exchange |
| [src/discover.py](src/discover.py) | Hashtag rotation, filtering, queueing |
| [src/images.py](src/images.py) | JPEG conversion, aspect/width clamping, 8MB fit |
| [src/caption.py](src/caption.py) | Caption assembly + best-effort handle extraction |
| [src/publish.py](src/publish.py) | Container create → poll → publish, all guards |
| [src/store.py](src/store.py) | Config, hashtag budget, dedupe ledger, queue, rate log |
| [src/refresh_token.py](src/refresh_token.py) | Token rotation, self-updates the secret |
| [config.yml](config.yml) | Hashtags, filters, captions, cadence, kill switch |

## Controls

**Stop all posting:** set `enabled: false` in [config.yml](config.yml) and push.

**Cadence:** `publish.daily_cap` (default 6) and `publish.min_gap_minutes`
(default 90) are the real limits. Meta's ceiling is 100/24h; the local guards sit
far below it deliberately.

**Hashtags:** Meta allows **30 unique tags per rolling 7 days**.
[src/store.py](src/store.py) tracks the window in `state/hashtag_queries.json`
and refuses to exceed it rather than letting the API error mid-run.

## Local testing

```bash
pip install -r requirements.txt
export IG_USER_ID=... IG_ACCESS_TOKEN=...

python -m src.refresh_token --check-only   # token valid? when does it expire?
python -m src.discover --dry-run --verbose # what would be queued?
python -m src.publish  --dry-run --verbose # exact caption + image URL
```

## Risk, stated once

This posts other people's photos automatically, with attribution only when
recoverable, and re-hosts their images. Attribution is not a copyright license.
Realistic outcomes include DMCA notices, the Meta app being disabled, or the
Instagram account being actioned. `queue/pending/` is plain JSON and inspectable
if you later want a human approval gate — that change is small and touches only
[src/publish.py](src/publish.py).
