# Skins Archive Monitor

Monitors the Skins Inclusive archive at `skins.nl/en/archives/` and sends a Telegram ping the moment new products appear. Runs on GitHub Actions every 5 minutes, costs €0, needs no machine of yours to be on.

## How it works

1. A cron job triggers a fresh Ubuntu container every 5 minutes.
2. Playwright (headless Chromium) logs into your Skins account and loads the archive.
3. The script extracts all products (name, URL, price, image) and diffs against the last known state stored in `state.json` in this repo.
4. New items → Telegram message per product with image, price, and direct link.
5. Updated `state.json` is committed back to the repo as the new baseline.

Extras:
- **First run:** sends a confirmation ping with the baseline count, no product spam.
- **Weekly heartbeat:** every Monday 09:00 UTC, pings you so you know the script is still alive even if no drops have happened.
- **Failure ping:** if scraping or login breaks, you get a Telegram alert instead of silence.
- **Manual trigger:** "Run workflow" button in the Actions tab for on-demand testing.

## Setup (≈15 minutes)

### 1. Create a Telegram bot

1. Open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`.
2. Give it a name and username when prompted. You'll get a token like `1234567890:AAH...`. **Save it** — this is your `TELEGRAM_BOT_TOKEN`.
3. Open a chat with your new bot and send it any message (e.g. "hi"). This is required before the bot can message you.

### 2. Get your Telegram chat ID

Message [@userinfobot](https://t.me/userinfobot) on Telegram. It replies with your numeric user ID — that's your `TELEGRAM_CHAT_ID`.

(Alternative: visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` after you messaged your bot, and find `"chat":{"id": <number>}` in the JSON.)

### 3. Create a public GitHub repo

1. On GitHub: **New repository** → make it **Public** → name it whatever (e.g. `skins-monitor`) → Create.

   Public is the right choice here: Actions minutes are unlimited on public repos (private would cost ~€4/month or force a 15+ min cadence). Your credentials live in encrypted Secrets, never in the code, so a public repo is safe. The only thing exposed is the code itself and `state.json` (a list of product URLs that are already public on Skins' site).
2. Upload all files from this folder to the repo (drag-and-drop in the GitHub web UI works, or use git):
   - `monitor.py`
   - `requirements.txt`
   - `.gitignore`
   - `.github/workflows/monitor.yml`
   - `README.md`

### 4. Add secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**. Add four:

| Name | Value |
|------|-------|
| `SKINS_EMAIL` | your Skins account email |
| `SKINS_PASSWORD` | your Skins account password |
| `TELEGRAM_BOT_TOKEN` | from step 1 |
| `TELEGRAM_CHAT_ID` | from step 2 |

Secrets are encrypted, never visible in code, and never printed in Actions logs.

### 5. First run

1. Go to the **Actions** tab in your repo.
2. If prompted, enable workflows.
3. Click **Skins Archive Monitor** in the left sidebar → **Run workflow** → **Run workflow**.
4. Wait ~1–2 minutes. Check Telegram — you should get the "✅ Skins monitor deployed" message with the baseline count.

From this point on, the cron takes over automatically.

## Expected behaviour

- **Quiet most of the time.** You only hear from the bot when new products drop, once a week as a heartbeat, or on failure.
- **5-minute cadence in theory, 5–15 min in practice.** GitHub's free-tier cron is best-effort, not real-time. If a product sells out in under 10 minutes, you may occasionally miss it.
- **No false negatives on simultaneous add+sellout.** The script compares the set of product IDs, not the total count — so if one item sells out while another drops in the same window, you still get notified about the new one.

## Troubleshooting

**No messages after manual run.**
Check the Actions tab → latest run → click through to the job logs. Most common causes: wrong Telegram token/chat ID, or you forgot to message your bot first.

**"Scraped 0 products" error.**
Skins may have changed their HTML. The failed run uploads a `debug-*.zip` artifact (visible in the run summary) containing the fetched HTML. Download it and share with me, and I'll update the selectors in `monitor.py`.

**Login failure.**
Same deal — check the debug artifact. Skins may have changed their login form, added a CAPTCHA, or your credentials are wrong.

**Repeated failure pings.**
Disable the schedule temporarily by commenting out the `schedule:` block in `monitor.yml`, investigate, fix, re-enable.

## Things to know

**Terms of service.** Most retailers' terms discourage automated access. At 5-minute intervals for personal use, practical risk is very low, but the theoretical worst case is account suspension. You're balancing that against the value of catching drops you'd otherwise miss.

**Rate limits / anti-bot.** If Skins starts challenging the login (CAPTCHA), the script will break and alert you. At that point, options are: longer interval (every 15+ min), cached cookies between runs, or residential proxy. None are implemented in v1 — add only if needed.

**Repo size over time.** The workflow commits `state.json` only when content changes, so the history stays small. No maintenance needed.

**Costs.** €0. GitHub Actions minutes are unlimited for public repos, so the 5-minute cadence runs forever at no cost. Telegram Bot API is free. The only ongoing cost is your Skins Inclusive membership, which you already have for other reasons.
