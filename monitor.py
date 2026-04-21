"""
Skins Archive monitor.

Logs into skins.nl, scrapes the /en/archives/ page, diffs against the previous
run's product list, and pings Telegram with any newly appeared items.
Run on GitHub Actions every 5 minutes.
"""

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

# --- Config from environment ---
# .strip() on every credential: trailing whitespace/newlines from copy-paste
# into GitHub Secrets is a common silent failure mode that results in the site
# rejecting the value as malformed.
SKINS_EMAIL = os.environ["SKINS_EMAIL"].strip()
SKINS_PASSWORD = os.environ["SKINS_PASSWORD"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
HEARTBEAT = os.environ.get("HEARTBEAT", "0") == "1"
MANUAL = os.environ.get("MANUAL", "0") == "1"

ARCHIVE_URL = "https://www.skins.nl/en/archives/"
LOGIN_URL = "https://www.skins.nl/en/account/login/"
STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")


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

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeout:
        pass
    log(f"login flow complete, now at {page.url}")
    await snapshot("05_after_login_complete")


async def scrape_products(page) -> list[dict]:
    """Load the archive and extract all product cards."""
    await page.goto(ARCHIVE_URL, wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)
    await dismiss_offcanvas(page)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeout:
        pass

    # Scroll to bottom until height stops changing — triggers any lazy-loaded products
    last_height = -1
    for _ in range(25):
        current_height = await page.evaluate("document.body.scrollHeight")
        if current_height == last_height:
            break
        last_height = current_height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)

    # Extract in the browser. Two strategies, first one that returns results wins.
    products = await page.evaluate(
        """
        () => {
            const results = [];
            const seen = new Set();
            const normalizeUrl = (u) => {
                try {
                    const url = new URL(u);
                    return url.origin + url.pathname.replace(/\\/$/, '');
                } catch { return u; }
            };

            // --- Strategy 1: JSON-LD structured data ---
            const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const script of ldScripts) {
                try {
                    const data = JSON.parse(script.textContent);
                    const items = Array.isArray(data) ? data : [data];
                    for (const item of items) {
                        const list = item['@type'] === 'ItemList'
                            ? (item.itemListElement || [])
                            : [item];
                        for (const entry of list) {
                            const product = entry.item || entry;
                            if (product['@type'] !== 'Product' || !product.url) continue;
                            const url = normalizeUrl(product.url);
                            const id = String(product.sku || product.productID || url);
                            if (seen.has(id)) continue;
                            seen.add(id);
                            const offers = product.offers;
                            let price = '';
                            if (offers) {
                                const o = Array.isArray(offers) ? offers[0] : offers;
                                if (o) price = o.price || o.priceSpecification?.price || '';
                            }
                            results.push({
                                id,
                                name: product.name || '',
                                url,
                                price: price ? `€${price}` : '',
                                image: Array.isArray(product.image) ? product.image[0] : (product.image || ''),
                                source: 'json-ld',
                            });
                        }
                    }
                } catch (e) {}
            }
            if (results.length > 0) return results;

            // --- Strategy 2: heuristic DOM scrape ---
            // Find anchors that point to a product page, have an image nearby, and a price.
            const priceRegex = /€\\s?\\d+[\\.,]?\\d{0,2}/;
            const anchors = document.querySelectorAll('a[href*="/en/"]');
            for (const a of anchors) {
                const href = a.href;
                if (!href) continue;
                if (href.includes('/archives') || href.includes('/account') || href.endsWith('/en/')) continue;

                const container = a.closest(
                    '[class*="product"], [class*="card"], [class*="Product"], article, li'
                );
                if (!container) continue;

                const img = container.querySelector('img');
                if (!img) continue;

                const text = container.textContent || '';
                const priceMatch = text.match(priceRegex);
                if (!priceMatch) continue;

                const url = normalizeUrl(href);
                if (seen.has(url)) continue;
                seen.add(url);

                const heading = container.querySelector('h1, h2, h3, h4, h5, h6');
                const name = (heading?.textContent || img.alt || '').trim();

                results.push({
                    id: url,
                    name,
                    url,
                    price: priceMatch[0],
                    image: img.src || img.dataset.src || '',
                    source: 'dom-heuristic',
                });
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
        price = html_escape(p.get("price") or "")
        url = p.get("url", "")
        img = p.get("image", "")
        caption = (
            f"<b>🆕 New in Skins Archive</b>\n\n"
            f"<b>{name}</b>\n"
            f"{price}\n\n"
            f'<a href="{url}">View product →</a>'
        )
        if img:
            await send_telegram_photo(img, caption)
        else:
            await send_telegram_message(caption)
        await asyncio.sleep(0.4)  # Telegram rate-limit headroom


# --- Main ---

async def run() -> None:
    DEBUG_DIR.mkdir(exist_ok=True)

    if STATE_FILE.exists():
        old_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        first_run = False
    else:
        old_state = []
        first_run = True

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()
        try:
            await login(page)
            products = await scrape_products(page)
        finally:
            try:
                (DEBUG_DIR / "last_page.html").write_text(
                    await page.content(), encoding="utf-8"
                )
            except Exception:
                pass
            await browser.close()

    if not products:
        raise RuntimeError(
            "Scraped 0 products — login or selectors likely need updating. "
            "Inspect debug/last_page.html (uploaded as workflow artifact on failure)."
        )

    old_ids = {item["id"] for item in old_state}
    new_products = [p for p in products if p["id"] not in old_ids]

    STATE_FILE.write_text(
        json.dumps(products, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if first_run:
        await send_telegram_message(
            f"✅ <b>Skins monitor deployed</b>\n\n"
            f"Baseline set: {len(products)} products currently in archive.\n"
            f"You'll get a ping here whenever new items appear.\n\n"
            f"<i>{now_str()}</i>"
        )
    elif new_products:
        await notify_new(new_products)

    if HEARTBEAT:
        await send_telegram_message(
            f"💓 <b>Weekly heartbeat</b>\n\n"
            f"Monitor is alive. Currently tracking {len(products)} products.\n\n"
            f"<i>{now_str()}</i>"
        )
    elif MANUAL and not first_run and not new_products:
        # Give feedback on manual test runs so you know it worked
        await send_telegram_message(
            f"🔧 <b>Manual run</b>\n\n"
            f"No new products. Archive has {len(products)} items.\n\n"
            f"<i>{now_str()}</i>"
        )

    print(
        f"OK. Total={len(products)} New={len(new_products)} "
        f"FirstRun={first_run} Heartbeat={HEARTBEAT} Manual={MANUAL}"
    )


def main() -> None:
    try:
        asyncio.run(run())
    except Exception as e:
        traceback.print_exc()
        try:
            asyncio.run(
                send_telegram_message(
                    f"⚠️ <b>Skins monitor failed</b>\n\n"
                    f"<pre>{html_escape(str(e))[:700]}</pre>\n\n"
                    f"<i>{now_str()}</i>"
                )
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
