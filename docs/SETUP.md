# Setup

End-to-end setup for `Datto91/ig-car-autopost`. Budget ~1 hour of clicking,
plus **days to weeks of waiting on Meta App Review and business verification**.

Read [Step 4](#step-4-app-review--business-verification-the-real-gate) first.
It is the step most likely to stop this project, and it is out of your control.

---

## What you already have

- An Instagram **Professional** (Business or Creator) account. Required.

## What you still need

| Thing | Why |
|---|---|
| A Facebook Page linked to the IG account | Hashtag Search only exists on "Instagram API with Facebook Login", which is Page-based |
| A Meta developer app | Holds the permissions and the token |
| `instagram_basic` + `pages_read_engagement` + `instagram_content_publish` | Read + publish |
| **Instagram Public Content Access** feature | Hashtag Search. **Needs App Review + business verification** |
| A public GitHub repo (or public media host) | Instagram cURLs your image URL; it must be publicly reachable |

---

## Step 1 — Link a Facebook Page

1. Create a Facebook Page for the car account if you don't have one.
2. On Instagram: **Settings → Account type and tools → Sharing to other apps
   → Facebook**, and connect the Page.
3. Verify in **Meta Business Suite** that the Page and IG account appear linked.

The API path we use reads Instagram *through* the Page. No Page, no hashtag search.

## Step 2 — Create the Meta app

1. Go to <https://developers.facebook.com/apps> → **Create App**.
2. App type: **Business**.
3. Add the **Instagram** product, then choose **Instagram API with Facebook Login**.

> Do not pick "Instagram API with Instagram Login". That flavor cannot do
> hashtag search. Meta's docs list Hashtag Search under the Facebook Login
> variant only.

4. Note your **App ID** and **App Secret** (App Settings → Basic).

## Step 3 — Request permissions

Under **App Review → Permissions and Features**, request:

- `instagram_basic`
- `pages_read_engagement`
- `instagram_content_publish`
- `pages_show_list`
- **Instagram Public Content Access** ← the hard one

## Step 4 — App Review + business verification (the real gate)

`instagram_content_publish` is a standard review: screencast, description, done.

**Instagram Public Content Access is different.** Meta's
[feature reference](https://developers.facebook.com/docs/features-reference/instagram-public-content-access)
says:

> This permission or feature is only available with **business verification**.
> You may also need to sign additional contracts before your app can access data.

That means submitting real legal business documents (registration, utility bill,
etc.) to Meta. The documented allowed usages are:

> discover content associated with your hashtag campaigns, understand public
> sentiment around your brand or identify contest, competition and sweepstakes
> entrants... provide customer support and better understand and manage your
> audience.

Note what is *not* on that list: sourcing other people's photos to republish.
Describe your use case honestly in the submission. If review is denied, hashtag
discovery is unavailable and you should switch to `business_discovery`
(see [ALTERNATIVES.md](ALTERNATIVES.md)) which needs **neither** App Review nor
business verification.

While in development mode you can test against **your own** account without
review, which is enough to validate the whole pipeline end to end.

## Step 5 — Get your IG user id

Graph API Explorer (<https://developers.facebook.com/tools/explorer/>), with
your app selected and the permissions granted:

```
GET /me/accounts
```

Take the Page id, then:

```
GET /{page-id}?fields=instagram_business_account
```

The returned `instagram_business_account.id` is your **`IG_USER_ID`**.

## Step 6 — Get a long-lived token

1. In Graph API Explorer, generate a **User access token** with all the
   permissions above. This one is short-lived (hours).
2. Exchange it for a long-lived (~60 day) token:

```bash
curl -s "https://graph.facebook.com/v23.0/oauth/access_token\
?grant_type=fb_exchange_token\
&client_id=YOUR_APP_ID\
&client_secret=YOUR_APP_SECRET\
&fb_exchange_token=SHORT_LIVED_TOKEN"
```

3. Confirm the expiry:

```bash
curl -s "https://graph.facebook.com/v23.0/debug_token\
?input_token=LONG_LIVED_TOKEN\
&access_token=YOUR_APP_ID|YOUR_APP_SECRET"
```

The `refresh-token` workflow keeps this alive automatically from here on.

## Step 7 — Public media hosting

`POST /media` does not accept file uploads. Meta's docs: *"We will cURL your
image using the passed in URL so it must be on a public server."*

`raw.githubusercontent.com` **only serves public repositories.** Private repo →
Instagram's fetcher gets a 404 → every post fails. Two supported layouts:

**A. Public repo (simplest).** Make this repo public. Secrets stay in Actions
secrets and are never committed, so the token is not exposed — but the whole
posting history and queue are world-readable.

**B. Private code repo + public media repo (recommended).** Keep this repo
private; create a second public repo, e.g. `Datto91/ig-car-media`, holding only
images. Then set the `MEDIA_BASE_URL` Actions **variable** to:

```
https://raw.githubusercontent.com/Datto91/ig-car-media/main
```

and adjust the discover workflow's commit step to push images there. Slightly
more wiring, much better privacy.

## Step 8 — Configure secrets

**Settings → Secrets and variables → Actions → Secrets:**

| Secret | Value |
|---|---|
| `IG_USER_ID` | From step 5 |
| `IG_ACCESS_TOKEN` | Long-lived token from step 6 |
| `FB_APP_ID` | App ID |
| `FB_APP_SECRET` | App Secret |
| `GH_PAT` | Fine-grained PAT, this repo only, **Secrets: read and write** |

`GH_PAT` is what lets the refresh job rewrite `IG_ACCESS_TOKEN`. Without it,
token rotation cannot persist and the bot stops at ~day 60.

**Variables** (optional): `MEDIA_BASE_URL` if using layout B.

## Step 9 — Enable Actions write access

**Settings → Actions → General → Workflow permissions** →
**Read and write permissions**. The discover/publish jobs commit state back.

## Step 10 — Test before going live

```bash
pip install -r requirements.txt
export IG_USER_ID=... IG_ACCESS_TOKEN=...

# 1. Confirm the token and see its expiry.
python -m src.refresh_token --check-only

# 2. Discover without writing anything.
python -m src.discover --dry-run --verbose

# 3. Discover for real (downloads + normalizes images).
python -m src.discover --limit 3

# 4. Show exactly what would post, without posting.
python -m src.publish --dry-run --verbose

# 5. Post one, for real.
python -m src.publish --count 1
```

Then run the workflows manually via **Actions → discover → Run workflow**
before trusting the cron.

---

## Operating it

**Stop everything:** set `enabled: false` in `config.yml`, push. Next run exits
without posting.

**Change cadence:** `publish.daily_cap` and `publish.min_gap_minutes` in
`config.yml` are the real limits. The cron just creates opportunities.

**Change hashtags:** edit `config.yml`, but respect the cap — Meta allows **30
unique hashtags per rolling 7 days**, and swapping tags mid-week burns fresh
slots. `state/hashtag_queries.json` tracks the window; `src/store.py` refuses to
exceed it rather than letting the API error.

## Known limits, by design

| Limit | Source |
|---|---|
| No author username from hashtag search | Meta: *"You cannot request the username field on returned media objects"* |
| 30 unique hashtags / 7 days | IG Hashtag Search docs |
| `recent_media` only covers last 24h | IG Hashtag Recent Media docs |
| 100 API posts / 24h | Content Publishing docs |
| JPEG only, no filters, no shopping tags | Content Publishing docs |
| Cannot edit or delete posts via API | Content Publishing docs |
| Cron delayed 5–30+ min, occasionally skipped | GitHub Actions scheduling |
| Schedules disabled after 60d repo inactivity | GitHub Actions |

## Attribution, stated plainly

Hashtag search returns no author handle. `src/caption.py` recovers a handle only
when the creator typed one into their own caption (self-tag, "shot by @x"). When
nothing is found, the credit line is omitted rather than faked.

Attribution is also not a copyright license. Reposting without permission can
result in DMCA notices, the Meta app being disabled, or the Instagram account
being actioned. This repo is configured for fully automatic posting because that
was the explicit requirement; `queue/pending/` is inspectable if you later want
a human gate, and `docs/ALTERNATIVES.md` documents the lower-risk sourcing path.
