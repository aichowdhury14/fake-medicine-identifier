"""
Scrapes MedEx.com.bd's brand directory into a local CSV drug database.

MedEx is Bangladesh's largest public medicine directory (30,000+ brands).
It is NOT the official DGDA registry (DGDA's own site has no working public
API and an expired/invalid SSL certificate at the time this was written),
so the resulting dataset represents "known brands listed in a public
pharmacy directory," not "officially DGDA-registered drugs." The app must
be honest about that distinction in its verdict wording.

Usage:
    python scrape_medex.py                # scrape all pages
    python scrape_medex.py --pages 50      # scrape first N pages only
    python scrape_medex.py --start 1 --pages 50 --out data/drugs_part1.csv
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://medex.com.bd/brands"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
REQUEST_DELAY_SECONDS = 0.5
MAX_RETRIES = 3


def fetch_page(page_num: int) -> str | None:
    url = f"{BASE_URL}?page={page_num}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            print(f"  [page {page_num}] attempt {attempt} failed: {exc}", file=sys.stderr)
            time.sleep(1.5 * attempt)
    return None


def parse_last_page(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    pagination = soup.select_one(".brand-list-pagination")
    if not pagination:
        return 1
    page_numbers = [
        int(a.get_text(strip=True))
        for a in pagination.select("a")
        if a.get_text(strip=True).isdigit()
    ]
    return max(page_numbers) if page_numbers else 1


def parse_brand_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for card in soup.select("a.brand-card"):
        href = card.get("href", "")
        match = re.search(r"/brands/(\d+)/", href)
        brand_id = match.group(1) if match else ""

        name_el = card.select_one(".brand-card__name")
        strength_el = card.select_one(".brand-card__strength")
        generic_el = card.select_one(".brand-card__generic")
        company_el = card.select_one(".brand-card__company")
        icon_el = card.select_one(".dosage-icon")

        rows.append({
            "brand_id": brand_id,
            "brand_name": name_el.get_text(strip=True) if name_el else "",
            "strength": strength_el.get_text(strip=True) if strength_el else "",
            "generic_name": generic_el.get_text(strip=True) if generic_el else "",
            "manufacturer": company_el.get_text(strip=True) if company_el else "",
            "dosage_form": icon_el.get("title", "") if icon_el else "",
            "source_url": href,
        })
    return rows


def scrape(start_page: int, end_page: int, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["brand_id", "brand_name", "strength", "generic_name",
                  "manufacturer", "dosage_form", "source_url"]

    seen_ids = set()
    write_header = not out_path.exists()
    with out_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        for page in range(start_page, end_page + 1):
            html = fetch_page(page)
            if html is None:
                print(f"[page {page}] giving up after {MAX_RETRIES} attempts, skipping", file=sys.stderr)
                continue

            rows = parse_brand_cards(html)
            new_rows = [r for r in rows if r["brand_id"] and r["brand_id"] not in seen_ids]
            for r in new_rows:
                seen_ids.add(r["brand_id"])
                writer.writerow(r)
            f.flush()

            print(f"[page {page}/{end_page}] +{len(new_rows)} rows (total this run: {len(seen_ids)})")
            time.sleep(REQUEST_DELAY_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=1, help="First page to scrape")
    parser.add_argument("--pages", type=int, default=None,
                         help="Number of pages to scrape from --start. Default: all pages found.")
    parser.add_argument("--out", type=str, default="../data/drugs.csv",
                         help="Output CSV path (appended to; run is resumable)")
    args = parser.parse_args()

    out_path = Path(__file__).parent / args.out

    print("Fetching page 1 to determine total page count...")
    first_html = fetch_page(1)
    if first_html is None:
        print("Could not reach MedEx. Aborting.", file=sys.stderr)
        sys.exit(1)
    last_page = parse_last_page(first_html)
    print(f"MedEx reports {last_page} total pages.")

    end_page = last_page if args.pages is None else min(args.start + args.pages - 1, last_page)
    print(f"Scraping pages {args.start}..{end_page} -> {out_path}")

    scrape(args.start, end_page, out_path)
    print("Done.")


if __name__ == "__main__":
    main()
