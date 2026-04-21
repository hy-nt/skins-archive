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
SKINS_EMAIL = os.environ["SKINS_EMAIL"]
SKINS_PASSWORD = os.environ["SKINS_PASSWORD"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
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


async def login(page) -> None:
    """Navigate to login page and submit credentials."""
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)

    email_selectors = [
        "input[type='email']",
        "input[name='email']",
        "input[name='login']",
        "#email",
    ]
    for sel in email_selectors:
        try:
            await page.locator(sel).first.fill(SKINS_EMAIL, timeout=2000)
            break
        except Exception:
            continue
    else:
        raise RuntimeError("Could not find email field on login page")

    pw_selectors = [
        "input[type='password']",
        "input[name='password']",
        "#password",
    ]
    for sel in pw_selectors:
        try:
            await page.locator(sel).first.fill(SKINS_PASSWORD, timeout=2000)
            break
        except Exception:
            continue
    else:
        raise RuntimeError("Could not find password field on login page")

    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Log in')",
        "button:has-text('Sign in')",
        "button:has-text('Inloggen')",
    ]
    for sel in submit_selectors:
        try:
            await page.locator(sel).first.click(timeout=2000)
            break
        except Exception:
            continue

    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeout:
        pass  # 'networkidle' can hang on modern sites; not fatal.


async def scrape_products(page) -> list[dict]:
    """Load the archive and extract all product cards."""
    await page.goto(ARCHIVE_URL, wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)
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
