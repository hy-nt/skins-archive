"""
Skins Archive monitor — GitHub Actions edition.

Logs into skins.nl, scrapes the /en/archives/ page, diffs against the previous
run's product list, and pings Telegram with any newly appeared items.
Credentials come from GitHub Secrets as environment variables (no .env file).
"""

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

# --- Paths (anchored to the script's own directory, not the CWD). ---
SCRIPT_DIR = Path(__file__).resolve().parent

STATE_FILE = SCRIPT_DIR / "state.json"
HEARTBEAT_FILE = SCRIPT_DIR / "last_heartbeat.txt"
DEBUG_DIR = SCRIPT_DIR / "debug"
# Persistent browser profile — stores cookies between runs so we don't have to
# log in every 5 minutes (which triggers bot detection). Delete this folder to
# force a fresh login.
USER_DATA_DIR = SCRIPT_DIR / ".browser-data"

# --- Config from environment (injected from GitHub Secrets by the workflow). ---
# .strip() on every credential: trailing whitespace/newlines from copy-paste
# is a classic silent-failure cause.
SKINS_EMAIL = os.environ["SKINS_EMAIL"].strip()
SKINS_PASSWORD = os.environ["SKINS_PASSWORD"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
# Manually triggered runs (from a terminal) get an extra confirmation ping.
MANUAL = os.environ.get("MANUAL", "0") == "1"
# GitHub Actions runners are headless-only (no display); force headless=True.
HEADLESS = True

ARCHIVE_URL = "https://www.skins.nl/en/archives/?p=1&order=release-date-ascending"
LOGIN_URL = "https://www.skins.nl/en/account/login/"
HEARTBEAT_INTERVAL = timedelta(days=7)


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def html_escape(s) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --- Playwright helpers ---

async def dismiss_cookie_banner(page) -> None:
    """Best-effort click on a cookie-consent button. Silent if nothing matches."""
    candidates = [
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('Alles accepteren')",
        "button:has-text('Accepteren')",
        "button:has-text('Akkoord')",
        "button:has-text('Allow all')",
        "[id*='cookie'] button",
        "[class*='cookie'] button:first-of-type",
    ]
    for selector in candidates:
        try:
            await page.locator(selector).first.click(timeout=1500)
            await page.wait_for_timeout(400)
            return
        except Exception:
            continue


async def dismiss_offcanvas(page) -> None:
    """Close any open Bootstrap offcanvas panel that might be intercepting clicks.

    Skins' site sometimes has a language picker or menu offcanvas open on load;
    its backdrop div (`.offcanvas-backdrop.show`) sits above the page and
    swallows pointer events, so any click on the login form silently fails.
    """
    # Fast path: check if a backdrop is actually present. If not, skip.
    backdrop_count = await page.locator(".offcanvas-backdrop.show").count()
    if backdrop_count == 0:
        return

    # Try 1: press Escape — standard Bootstrap dismiss
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_selector(
            ".offcanvas-backdrop.show", state="detached", timeout=3000
        )
        return
    except Exception:
        pass

    # Try 2: click the backdrop itself (also dismisses by default)
    try:
        await page.locator(".offcanvas-backdrop.show").first.click(timeout=2000, force=True)
        await page.wait_for_selector(
            ".offcanvas-backdrop.show", state="detached", timeout=3000
        )
        return
    except Exception:
        pass

    # Try 3: look for a close button inside any visible offcanvas panel
    try:
        await page.locator(
            ".offcanvas.show .btn-close, .offcanvas.show [data-bs-dismiss='offcanvas']"
        ).first.click(timeout=2000)
        await page.wait_for_selector(
            ".offcanvas-backdrop.show", state="detached", timeout=3000
        )
        return
    except Exception:
        pass

    # Try 4: nuke it from the DOM directly — ugly but reliable as a last resort
    await page.evaluate(
        """
        () => {
            document.querySelectorAll('.offcanvas-backdrop').forEach(el => el.remove());
            document.querySelectorAll('.offcanvas.show').forEach(el => {
                el.classList.remove('show');
                el.style.display = 'none';
            });
            document.body.classList.remove('offcanvas-backdrop-open');
            document.body.style.overflow = '';
        }
        """
    )


async def login(page) -> None:
    """Navigate to login page and submit credentials.

    Skins uses a progressive two-step login: enter email -> click Continue ->
    password field becomes visible -> enter password -> click Continue again.
    The password field is wrapped in a `.password-field-wrapper.d-none` div
    that only becomes visible after the email step.
    """

    def log(msg: str) -> None:
        print(f"[login] {msg}", flush=True)

    async def snapshot(label: str) -> None:
        """Save a screenshot + HTML with a label. Saved to debug/ dir which is
        uploaded as a workflow artifact on failure."""
        try:
            await page.screenshot(path=str(DEBUG_DIR / f"{label}.png"), full_page=True)
            (DEBUG_DIR / f"{label}.html").write_text(
                await page.content(), encoding="utf-8"
            )
            log(f"snapshot saved: {label}")
        except Exception as e:
            log(f"snapshot failed for {label}: {e}")

    log(f"navigating to {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    log(f"landed on {page.url}")
    await dismiss_cookie_banner(page)
    await dismiss_offcanvas(page)
    await snapshot("01_initial_login_page")

    login_form = "form.login-form"
    email_field = f"{login_form} input#loginMail"
    password_field = f"{login_form} input#loginPassword"
    submit_button = f"{login_form} button[type='submit']"

    # Verify login form is actually present before we try to fill
    count = await page.locator(email_field).count()
    log(f"found {count} email field(s) matching {email_field}")
    if count == 0:
        raise RuntimeError(
            "Login form email field not found. Skins may have changed the page layout. "
            "See debug/01_initial_login_page.html"
        )

    # Step 1: fill email
    log(f"filling email ({len(SKINS_EMAIL)} chars, first char ord={ord(SKINS_EMAIL[0]) if SKINS_EMAIL else 0})")
    await page.locator(email_field).fill(SKINS_EMAIL, timeout=10000)
    # Fire a blur event — some validators only run on blur, not on .fill()
    await page.locator(email_field).evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")
    await page.wait_for_timeout(500)
    await snapshot("02_after_email_filled")

    # Step 2: click Continue
    await dismiss_offcanvas(page)  # defensive: in case something opened during fill
    log("clicking Continue (step 1)")
    await page.locator(submit_button).first.click()
    await page.wait_for_timeout(1500)  # give the AJAX call room to fire
    await snapshot("03_after_first_continue")

    # Step 3: wait for the password field to become visible
    try:
        await page.locator(password_field).wait_for(state="visible", timeout=15000)
        log("password field now visible")
    except PlaywrightTimeout:
        await snapshot("04_password_never_appeared")
        # Gather diagnostics
        register_visible = await page.locator(
            "div.register-card:not(.d-none)"
        ).count()
        if register_visible:
            raise RuntimeError(
                "After email step, Skins showed the register form instead of the "
                "password field. This usually means the email is not registered. "
                "Double-check SKINS_EMAIL matches the account you use on skins.nl."
            )
        # Any visible validation message anywhere on the page?
        vis_errors = await page.locator(
            ".invalid-feedback:visible, .form-field-feedback:visible, .alert:visible"
        ).all_inner_texts()
        vis_errors = [m.strip() for m in vis_errors if m.strip()]
        # Also check the is-invalid class on email field
        email_classes = await page.locator(email_field).get_attribute("class") or ""
        # And current URL — did we get redirected somewhere?
        current_url = page.url
        raise RuntimeError(
            f"Password field did not appear within 15s after email step. "
            f"Current URL: {current_url}. Email field classes: '{email_classes}'. "
            f"Visible errors: {vis_errors or '(none)'}. "
            f"See debug/ screenshots for visual state."
        )

    # Step 4: fill password and submit
    log("filling password")
    await page.locator(password_field).fill(SKINS_PASSWORD, timeout=5000)
    await dismiss_offcanvas(page)  # defensive
    log("clicking Continue (step 2)")
    await page.locator(submit_button).first.click()

    # Wait for the REAL success signal: a navigation away from /account/login/.
    # The old `wait_for_load_state("networkidle")` silently timed out on Skins'
    # constant analytics traffic and let us proceed past a failed login.
    try:
        await page.wait_for_url(
            lambda url: "/account/login" not in url, timeout=20000
        )
        log(f"login succeeded, redirected to {page.url}")
    except PlaywrightTimeout:
        # Didn't redirect — login almost certainly failed. Gather diagnostics.
        await snapshot("05a_login_stuck")

        # Look for the site's actual rejection message. "Password incorrect"
        # text is present as a dormant template in the DOM even on success, so
        # we check for the `.is-invalid` class being applied to the field or
        # for feedback divs that are actually visible.
        visible_errors = await page.locator(
            ".invalid-feedback:visible, .form-field-feedback:visible, "
            ".alert:visible, .flashbag:visible"
        ).all_inner_texts()
        visible_errors = [e.strip() for e in visible_errors if e.strip()]

        # Check is-invalid classes on the two form fields
        email_class = await page.locator(email_field).get_attribute("class") or ""
        pw_class = await page.locator(password_field).get_attribute("class") or ""

        current_url = page.url
        raise RuntimeError(
            f"Login did not redirect within 20s — almost certainly a credential "
            f"issue or bot challenge. Current URL: {current_url}. "
            f"Email field classes: '{email_class}'. "
            f"Password field classes: '{pw_class}'. "
            f"Visible errors on page: {visible_errors or '(none)'}. "
            f"See debug/05a_login_stuck.png."
        )

    log(f"login flow complete, now at {page.url}")
    await snapshot("05_after_login_complete")


async def scrape_products(page) -> list[dict]:
    """Extract all products from the archive page.

    Skins uses server-rendered HTML with Bootstrap-based card markup:
      <div class="cms-listing-col" itemscope itemtype="schema.org/ListItem">
        <div class="card product-box" data-product-information='{"id":"...","name":"..."}'>
          <a class="product-image-link" href="https://.../en/.../product-slug/">
            <img class="product-image" src="..." alt="...">
          </a>
          ... <span class="product-cheapest-price-price">€11</span>
          ... <span class="list-price-price striketrough">€39</span>
        </div>
      </div>

    The archive paginates (22 pages of 24 products = 526). For monitoring
    purposes we only fetch page 1: new drops appear here first, and scraping
    22 pages every 5 minutes would be unnecessarily aggressive.
    """
    if "/archives" not in page.url:
        await page.goto(ARCHIVE_URL, wait_until="domcontentloaded")
        await dismiss_cookie_banner(page)
        await dismiss_offcanvas(page)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeout:
        pass

    # Wait for the product grid to render (defensive against slow loads).
    try:
        await page.wait_for_selector(
            ".cms-listing-col .product-box", state="attached", timeout=10000
        )
    except PlaywrightTimeout:
        # Grid never appeared — could be legitimately empty archive, or a page
        # error. Proceed anyway; caller handles the 0-products case.
        pass

    products = await page.evaluate(
        """
        () => {
            const results = [];
            const cards = document.querySelectorAll('.cms-listing-col .product-box');

            for (const card of cards) {
                let id = '', name = '';
                const raw = card.getAttribute('data-product-information');
                if (raw) {
                    try {
                        const info = JSON.parse(raw);
                        id = String(info.id || '');
                        name = info.name || '';
                    } catch (e) {}
                }

                // Fallback for id/name if the data attribute is missing or malformed
                const link = card.querySelector('a.product-image-link, a[href*="/en/"]');
                const url = link ? link.href : '';
                if (!name && link) name = (link.getAttribute('title') || '').trim();
                if (!id) id = url;  // URL is a stable fallback identifier

                if (!id || !url) continue;  // Skip malformed cards

                // Sale price (what the customer pays right now)
                const saleEl = card.querySelector('.product-cheapest-price-price');
                const listEl = card.querySelector('.list-price-price');
                const discountEl = card.querySelector('.list-price-percentage');

                const price_current = saleEl ? saleEl.textContent.trim() : '';
                const price_original = listEl ? listEl.textContent.trim() : '';
                const discount = discountEl ? discountEl.textContent.trim() : '';

                // Build a display price string
                let price = price_current;
                if (price_current && price_original) {
                    price = `${price_current} (was ${price_original})`;
                }

                // Image — prefer the largest from srcset if available
                const img = card.querySelector('img.product-image, img');
                let image = '';
                if (img) {
                    // Use plain src; srcset parsing is fragile and Telegram
                    // handles Skins' CDN URLs fine as-is.
                    image = img.src || img.getAttribute('data-src') || '';
                }

                results.push({ id, name, url, price, price_current,
                               price_original, discount, image });
            }

            return results;
        }
        """
    )
    return products


# --- Telegram ---

async def send_telegram_message(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        r.raise_for_status()


async def send_telegram_photo(photo_url: str, caption: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "photo": photo_url,
                "caption": caption[:1024],
                "parse_mode": "HTML",
            },
        )
        if r.status_code >= 400:
            # Image fetch/format can fail on Telegram's side — fall back to text only
            await send_telegram_message(caption)
            return
        r.raise_for_status()


async def notify_new(products: list[dict]) -> None:
    for p in products:
        name = html_escape(p.get("name") or "Unknown product")
        price_current = html_escape(p.get("price_current") or "")
        price_original = html_escape(p.get("price_original") or "")
        discount = html_escape(p.get("discount") or "")
        url = p.get("url", "")
        img = p.get("image", "")

        # Build a nicely formatted price line. Three states:
        #   both prices known  → "<b>€11</b>  <s>€39</s>  (71.79% DISCOUNT)"
        #   only current known → "<b>€11</b>"
        #   nothing            → ""  (unlikely, but handle gracefully)
        if price_current and price_original:
            price_line = f"<b>{price_current}</b>  <s>{price_original}</s>"
            if discount:
                price_line += f"  <i>{discount}</i>"
        elif price_current:
            price_line = f"<b>{price_current}</b>"
        else:
            price_line = html_escape(p.get("price") or "")

        caption = (
            f"<b>🆕☁️ New in Skins Archive</b>\n\n"
            f"<b>{name}</b>\n"
            f"{price_line}\n\n"
            f'<a href="{url}">View product →</a>'
        )
        if img:
            await send_telegram_photo(img, caption)
        else:
            await send_telegram_message(caption)
        await asyncio.sleep(0.4)  # Telegram rate-limit headroom


# --- Main ---

# JS that patches out the most common headless-browser detection signals.
# Injected into every page before any other scripts run, so the site's
# fingerprinting code sees a normal-looking browser.
STEALTH_INIT_SCRIPT = """
// Hide navigator.webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Fake a realistic plugins array (headless Chrome has empty plugins)
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'PDF Viewer', description: '', filename: 'internal-pdf-viewer' },
        { name: 'Chrome PDF Viewer', description: '', filename: 'internal-pdf-viewer' },
        { name: 'Chromium PDF Viewer', description: '', filename: 'internal-pdf-viewer' },
        { name: 'Microsoft Edge PDF Viewer', description: '', filename: 'internal-pdf-viewer' },
        { name: 'WebKit built-in PDF', description: '', filename: 'internal-pdf-viewer' },
    ],
});

// Set a realistic language list
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en', 'nl'],
});

// Add window.chrome — headless Chrome is missing this object entirely
if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };
}

// Patch permissions query — headless returns inconsistent results
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
}
"""


async def check_archive_state(page) -> str:
    """Navigate to the archive and classify the resulting state.

    Returns:
        'success'        - we're on /en/archives/ with (presumably) the product grid
        'login_required' - redirected to /account/login/ (no valid session)
        'access_denied'  - redirected elsewhere (typically /how-to-archive),
                           meaning we're logged in but the session isn't being
                           treated as having Inclusive access
    """
    await page.goto(ARCHIVE_URL, wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)
    await dismiss_offcanvas(page)
    url = page.url.lower()
    if "/account/login" in url:
        return "login_required"
    if "how-to-archive" in url:
        return "access_denied"
    if "/archives" in url:
        return "success"
    return "access_denied"  # unknown redirect; treat as denied


async def run() -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    USER_DATA_DIR.mkdir(exist_ok=True)

    if STATE_FILE.exists():
        old_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        first_run = False
    else:
        old_state = []
        first_run = True

    async with async_playwright() as p:
        # Persistent context: cookies, localStorage, etc. survive between runs.
        # This is the single biggest anti-detection measure — on most runs we
        # reuse the existing session and never hit the login endpoint at all,
        # which is where Skins' bot detection is strongest.
        #
        # channel="chrome" uses the user's installed Chrome rather than the
        # bundled Chromium. Chrome has a different (more legitimate) version
        # string and fingerprint that many bot-detection services whitelist.
        # If Chrome isn't installed, falls back to bundled Chromium.
        launch_kwargs = dict(
            user_data_dir=str(USER_DATA_DIR),
            headless=HEADLESS,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Europe/Amsterdam",
            viewport={"width": 1366, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials",
            ],
        )
        try:
            context = await p.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
            print("[main] using installed Chrome (channel=chrome)", flush=True)
        except Exception as e:
            # Chrome not installed / Playwright can't find it — use bundled Chromium.
            print(f"[main] chrome channel unavailable ({e}), falling back to bundled Chromium", flush=True)
            context = await p.chromium.launch_persistent_context(**launch_kwargs)
        # Apply stealth patches to every page in this context
        await context.add_init_script(STEALTH_INIT_SCRIPT)

        page = context.pages[0] if context.pages else await context.new_page()
        try:
            # Try the archive directly first. Most runs this works because
            # the persistent cookie jar still has a valid session.
            print("[main] trying archive directly with cached session", flush=True)
            state = await check_archive_state(page)
            print(f"[main] initial archive state: {state} (url={page.url})", flush=True)

            if state in ("login_required", "access_denied"):
                # Either no session, or session exists but isn't being treated
                # as having Inclusive access. Both can be recovered by a fresh
                # login — stale/incomplete cookies from prior failed runs will
                # get overwritten. Only escalate to a hard error if login
                # itself fails or still doesn't unlock the archive.
                reason = "no session" if state == "login_required" else "session exists but no archive access"
                print(f"[main] {reason}, running login flow", flush=True)
                await login(page)
                state = await check_archive_state(page)
                print(f"[main] archive state after login: {state} (url={page.url})", flush=True)

            if state == "access_denied":
                raise RuntimeError(
                    f"Completed login but still redirected to {page.url} "
                    f"instead of the archive. Either the account doesn't have "
                    f"active Inclusive membership, or bot detection is "
                    f"downgrading the session. See README for next steps "
                    f"(try HEADLESS=0 in .env)."
                )
            if state != "success":
                raise RuntimeError(f"Unexpected archive state: {state} at {page.url}")

            products = await scrape_products(page)
        finally:
            try:
                (DEBUG_DIR / "last_page.html").write_text(
                    await page.content(), encoding="utf-8"
                )
            except Exception:
                pass
            await context.close()

    if not products:
        raise RuntimeError(
            "Scraped 0 products — login or selectors likely need updating. "
            f"Inspect {DEBUG_DIR}\\last_page.html."
        )

    old_ids = {item["id"] for item in old_state}
    new_products = [p for p in products if p["id"] not in old_ids]

    STATE_FILE.write_text(
        json.dumps(products, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if first_run:
        await send_telegram_message(
            f"✅☁️ <b>Skins monitor deployed</b>\n\n"
            f"Baseline set: {len(products)} products currently in archive.\n"
            f"You'll get a ping here whenever new items appear.\n\n"
            f"<i>{now_str()}</i>"
        )
    elif new_products:
        await notify_new(new_products)

    # Self-contained heartbeat: fire a "monitor is alive" ping every 7 days.
    # We track this via last_heartbeat.txt rather than state.json to keep state
    # file format simple (just a product list).
    now = datetime.now(timezone.utc)
    last_hb_str = HEARTBEAT_FILE.read_text().strip() if HEARTBEAT_FILE.exists() else ""
    try:
        last_hb = datetime.fromisoformat(last_hb_str) if last_hb_str else None
    except ValueError:
        last_hb = None
    should_heartbeat = last_hb is None or (now - last_hb) >= HEARTBEAT_INTERVAL

    if should_heartbeat and not first_run:
        await send_telegram_message(
            f"💓☁️ <b>Weekly heartbeat</b>\n\n"
            f"Monitor is alive. Currently tracking {len(products)} products.\n\n"
            f"<i>{now_str()}</i>"
        )
        HEARTBEAT_FILE.write_text(now.isoformat())
    elif first_run:
        # Seed the heartbeat clock on first run so we don't ping immediately.
        HEARTBEAT_FILE.write_text(now.isoformat())
    elif MANUAL and not new_products:
        # Give feedback on manual test runs so you know it worked
        await send_telegram_message(
            f"☁️🔧 <b>Manual run</b>\n\n"
            f"☁️ No new products. Archive has {len(products)} items.\n\n"
            f"<i>{now_str()}</i>"
        )

    print(
        f"OK. Total={len(products)} New={len(new_products)} "
        f"FirstRun={first_run} Heartbeat={should_heartbeat} Manual={MANUAL}"
    )


def main() -> None:
    try:
        asyncio.run(run())
    except Exception as e:
        traceback.print_exc()
        try:
            asyncio.run(
                #send_telegram_message(
                   # f"⚠️☁️ <b>Skins monitor failed</b>\n\n"
                  #  f"<pre>{html_escape(str(e))[:700]}</pre>\n\n"
                   # f"<i>{now_str()}</i>"
                )
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
