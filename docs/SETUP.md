# Setup

End-to-end setup for `Datto91/ig-car-autopost`. Budget ~1 hour of clicking.

**No App Review. No business verification.** Because this app only ever acts on
*your own* Instagram account, it runs on **Standard Access**, which every
Business-type app gets automatically. From
[Meta's Access Levels doc](https://developers.facebook.com/docs/graph-api/overview/access-levels):

> All Business, Consumer, and Gaming apps are **automatically approved for
> Standard Access** for all permissions and features.
>
> If your app will only be used by people who have a role on it, the permissions
> and features your app requires will only need Standard Access.

App Review and business verification gate **Advanced Access** — the level you'd
need to act on *other people's* accounts. You don't. See
[Step 4](#step-4-access-levels-why-you-dont-need-app-review).

---

## What you already have

- An Instagram **Professional** (Business or Creator) account. Required.

## What you still need

| Thing | Why |
|---|---|
| A Facebook Page linked to the IG account | Hashtag Search only exists on "Instagram API with Facebook Login", which is Page-based |
| A Meta developer app, type **Business** | Holds the permissions and the token; Business type gets Standard Access automatically |
| Your IG account + Page on the app's roster | Standard Access features are active for **role users** only — that's what makes review unnecessary |
| `instagram_basic` + `pages_read_engagement` + `instagram_content_publish` | Read + publish |
| **Instagram Public Content Access** feature | Hashtag Search. Automatic at Standard Access |
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

## Step 4 — Access levels: why you don't need App Review

Every Graph API permission and feature has two access levels, and the difference
is *whose* accounts your app may act for:

| | Standard Access | Advanced Access |
|---|---|---|
| How you get it | **Automatic** for Business-type apps | App Review + business verification |
| Who your app can act for | Only users with a **role on the app** | Anyone |
| Hashtag Search | ✅ Works | ✅ Works |
| Content Publishing | ✅ Works | ✅ Works |

This project only ever touches **your own** Instagram account, so Standard
Access is sufficient and it is granted automatically. The scary language on the
[Instagram Public Content Access page](https://developers.facebook.com/docs/features-reference/instagram-public-content-access)
about App Review and business verification describes **Advanced Access only**.

**The one thing you must do:** make sure the Instagram account and its linked
Page are on your app's roster — **App Dashboard → App Roles → Roles**. You are
admin of your own app by default, so this is usually already true. If the
account isn't a role user, Standard Access features silently won't work for it.

### Keep the app in Development mode

Do **not** switch to Live mode. Standard Access features are active for role
users in Development mode, which is exactly your setup, and Meta's own
[app modes doc](https://developers.facebook.com/docs/development/build-and-test/app-modes)
advises staying there until development is complete. Switching to Live gains you
nothing here and makes previously-private test posts visible to everyone.

> ⚠️ **Development mode is not a sandbox.** Posts published from a dev-mode app
> go to your real, public Instagram feed and real followers see them. The
> dev/live distinction controls *whose accounts your app may act for*, not
> whether the posting is real. Use `--dry-run` for rehearsals.

### When you *would* need Advanced Access

Only if this stopped being a single-account tool — e.g. you offered it as a
service posting to other people's accounts. That is a different product with a
different risk profile; don't drift into it accidentally.

If hashtag search ever does fail for a reason you can't resolve,
[ALTERNATIVES.md](ALTERNATIVES.md) documents the `business_discovery` swap,
which returns author usernames and also works at Standard Access.

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

| Secret | Value | Set? |
|---|---|---|
| `IG_USER_ID` | From step 5 | ✅ `17841400962074653` |
| `IG_ACCESS_TOKEN` | Long-lived token from step 6 | ⬜ |
| `FB_APP_ID` | App ID (App Settings → Basic) | ⬜ |
| `FB_APP_SECRET` | App Secret (same page, click Show) | ⬜ |
| `GH_PAT` | Fine-grained PAT, this repo only, **Secrets: read and write** | ⬜ |

`GH_PAT` is what lets the refresh job rewrite `IG_ACCESS_TOKEN`. Without it,
token rotation cannot persist and the bot stops working at ~day 60.

### Set them without leaking them

Run these yourself. Each command **prompts** for the value, so the secret never
lands in your shell history, in this repo, or in a chat log:

```bash
gh secret set IG_ACCESS_TOKEN --repo Datto91/ig-car-autopost
gh secret set FB_APP_ID       --repo Datto91/ig-car-autopost
gh secret set FB_APP_SECRET   --repo Datto91/ig-car-autopost
gh secret set GH_PAT          --repo Datto91/ig-car-autopost
```

Or paste them into the web UI:
<https://github.com/Datto91/ig-car-autopost/settings/secrets/actions>

> ⚠️ **Never paste a token into a chat, an issue, a commit, or a code comment.**
> An `IG_ACCESS_TOKEN` grants full read + publish rights to your Instagram
> account. If one is ever exposed, revoke it immediately by regenerating in the
> [Graph API Explorer](https://developers.facebook.com/tools/explorer/) or
> resetting the App Secret — that invalidates every token derived from it.
>
> **Token shape check:** a working token here starts with **`EAA`** (Facebook
> Login). A token starting with `IGAA` is an *Instagram Login* token and
> **cannot do hashtag search** — see Step 2.

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
| Only works for accounts with a **role on the app** | Standard Access definition — fine here, it's your own account |
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
