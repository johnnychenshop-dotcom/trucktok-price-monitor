from __future__ import annotations

import csv
import logging
import os
import smtplib
import sys
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = os.getenv("BASE_URL", "https://www.trucktok.com").rstrip("/")
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
PRICES_FILE = Path(os.getenv("PRICES_FILE", "prices.csv"))
CHANGES_FILE = Path(os.getenv("CHANGES_FILE", "price_changes.csv"))
TIMEZONE = os.getenv("MONITOR_TIMEZONE", "Asia/Shanghai")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
MAX_PRODUCTS = int(os.getenv("MAX_PRODUCTS", "0"))
CATALOG_PAGE_SIZE = 250

PRICE_FIELDS = ["date", "product_name", "url", "price", "stock_status"]
CHANGE_FIELDS = [
    "date",
    "product_name",
    "url",
    "old_price",
    "new_price",
    "change_amount",
    "change_percent",
]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("trucktok-price-monitor")
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


@dataclass(frozen=True)
class Product:
    product_name: str
    url: str
    price: Decimal
    stock_status: str


def build_session() -> requests.Session:
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (compatible; TruckTokPriceMonitor/1.0; "
                "+https://github.com/)"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def get_soup(
    session: requests.Session, url: str, parser: str = "html.parser"
) -> BeautifulSoup:
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.content, parser)


def discover_product_urls(session: requests.Session) -> list[str]:
    root = get_soup(session, SITEMAP_URL)
    product_sitemaps = [
        loc.get_text(strip=True)
        for loc in root.find_all("loc")
        if "sitemap_products" in loc.get_text()
    ]
    if not product_sitemaps:
        raise RuntimeError(f"No product sitemap found in {SITEMAP_URL}")

    product_urls: set[str] = set()
    for sitemap in product_sitemaps:
        soup = get_soup(session, sitemap)
        for loc in soup.find_all("loc"):
            url = loc.get_text(strip=True)
            if "/products/" in url:
                product_urls.add(url.split("?", 1)[0].rstrip("/"))

    urls = sorted(product_urls)
    if MAX_PRODUCTS > 0:
        urls = urls[:MAX_PRODUCTS]
    if not urls:
        raise RuntimeError("No product URLs found")
    return urls


def scrape_all_products(session: requests.Session, urls: Iterable[str]) -> list[Product]:
    url_list = list(urls)
    expected_handles = {
        unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]) for url in url_list
    }
    products_by_handle: dict[str, Product] = {}

    # Shopify exposes the storefront catalog in pages of up to 250 products.
    # This reduces roughly 622 product requests to only 3-4 catalog requests and
    # avoids triggering the storefront's HTTP 429 rate limit.
    page = 1
    while True:
        response = session.get(
            f"{BASE_URL}/products.json",
            params={"limit": CATALOG_PAGE_SIZE, "page": page},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        catalog_items = response.json().get("products", [])
        if not catalog_items:
            break

        for item in catalog_items:
            handle = str(item.get("handle", "")).strip()
            if handle not in expected_handles:
                continue
            variants = item.get("variants") or []
            if not variants:
                continue
            prices = [
                Decimal(str(variant["price"]))
                for variant in variants
                if variant.get("price") not in (None, "")
            ]
            if not prices:
                continue
            available = any(bool(variant.get("available")) for variant in variants)
            products_by_handle[handle] = Product(
                product_name=str(item["title"]).strip(),
                url=f"{BASE_URL}/products/{quote(handle, safe='-._~')}",
                price=min(prices),
                stock_status="in_stock" if available else "out_of_stock",
            )

        LOGGER.info(
            "Fetched catalog page %s (%s items, %s/%s matched)",
            page,
            len(catalog_items),
            len(products_by_handle),
            len(expected_handles),
        )
        if len(catalog_items) < CATALOG_PAGE_SIZE:
            break
        page += 1

    missing = sorted(expected_handles - products_by_handle.keys())
    if missing:
        preview = "\n".join(
            f"{BASE_URL}/products/{quote(handle, safe='-._~')}" for handle in missing[:10]
        )
        raise RuntimeError(
            f"Catalog was missing {len(missing)} of {len(url_list)} products. "
            f"No CSV files were changed.\n{preview}"
        )
    return sorted(products_by_handle.values(), key=lambda item: item.url)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def format_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def update_prices(run_date: date, products: list[Product]) -> list[dict[str, str]]:
    existing = read_csv(PRICES_FILE)
    today = run_date.isoformat()
    # A rerun on the same day replaces that day's snapshot instead of duplicating it.
    kept = [row for row in existing if row.get("date") != today]
    current = [
        {
            "date": today,
            "product_name": product.product_name,
            "url": product.url,
            "price": format_money(product.price),
            "stock_status": product.stock_status,
        }
        for product in products
    ]
    write_csv(PRICES_FILE, PRICE_FIELDS, [*kept, *current])
    return existing


def find_previous_snapshot(
    existing: list[dict[str, str]], run_date: date
) -> dict[str, dict[str, str]]:
    earlier_dates = sorted(
        {
            row["date"]
            for row in existing
            if row.get("date") and row["date"] < run_date.isoformat()
        }
    )
    if not earlier_dates:
        return {}
    previous_date = earlier_dates[-1]
    LOGGER.info("Comparing against snapshot from %s", previous_date)
    return {
        row["url"]: row
        for row in existing
        if row.get("date") == previous_date and row.get("url")
    }


def calculate_changes(
    run_date: date,
    products: list[Product],
    previous: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for product in products:
        old_row = previous.get(product.url)
        if not old_row:
            continue
        old_price = Decimal(old_row["price"])
        if old_price == product.price:
            continue
        amount = product.price - old_price
        percent = (
            (amount / old_price * Decimal(100)) if old_price else Decimal(0)
        )
        changes.append(
            {
                "date": run_date.isoformat(),
                "product_name": product.product_name,
                "url": product.url,
                "old_price": format_money(old_price),
                "new_price": format_money(product.price),
                "change_amount": format_money(amount),
                "change_percent": f"{percent.quantize(Decimal('0.01'))}%",
            }
        )
    return changes


def update_changes_file(run_date: date, changes: list[dict[str, str]]) -> None:
    existing = read_csv(CHANGES_FILE)
    today = run_date.isoformat()
    kept = [row for row in existing if row.get("date") != today]
    write_csv(CHANGES_FILE, CHANGE_FIELDS, [*kept, *changes])


def send_email(changes: list[dict[str, str]], run_date: date) -> None:
    if not changes:
        LOGGER.info("No price changes; email skipped")
        return

    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        LOGGER.warning(
            "Price changes found, but email is not configured. Missing: %s",
            ", ".join(missing),
        )
        return

    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    recipient = os.environ["EMAIL_TO"]
    sender = os.getenv("EMAIL_FROM", user)

    lines = [f"TruckTok price changes on {run_date.isoformat()}:", ""]
    for item in changes:
        direction = "UP" if Decimal(item["change_amount"]) > 0 else "DOWN"
        lines.extend(
            [
                f"[{direction}] {item['product_name']}",
                f"{item['old_price']} -> {item['new_price']} "
                f"({item['change_percent']})",
                item["url"],
                "",
            ]
        )

    message = EmailMessage()
    message["Subject"] = f"TruckTok price alert: {len(changes)} change(s)"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("\n".join(lines))

    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(host, port, timeout=30) as smtp:
        if not use_ssl:
            smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)
    LOGGER.info("Price alert sent to %s", recipient)


def main() -> int:
    try:
        run_date = datetime.now(ZoneInfo(TIMEZONE)).date()
        session = build_session()
        urls = discover_product_urls(session)
        LOGGER.info("Discovered %s product URLs", len(urls))
        products = scrape_all_products(session, urls)

        existing = read_csv(PRICES_FILE)
        previous = find_previous_snapshot(existing, run_date)
        changes = calculate_changes(run_date, products, previous)

        update_prices(run_date, products)
        update_changes_file(run_date, changes)
        send_email(changes, run_date)
        LOGGER.info(
            "Done: %s products, %s price changes", len(products), len(changes)
        )
        return 0
    except Exception:
        LOGGER.exception("Price monitor failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
