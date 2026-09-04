"""
Fetches general medical reference info (indications, contraindications,
side effects, pregnancy/lactation warnings) for a specific medicine from
its MedEx detail page, on demand, with a local disk cache.

Scope is deliberately narrow: this surfaces the standard, textbook
use-case of a known drug (e.g. "Paracetamol: pain relief, fever
reduction") as reference information only. It never accepts a symptom
and suggests a medicine — that's diagnosis-adjacent and out of scope for
a directory/identity-checking tool. Every response carries a
"consult a doctor or pharmacist" caveat for exactly that reason.

Fetched on demand (not bulk-scraped) because pre-fetching detail pages
for all 25,000+ catalog entries would take hours and load MedEx's server
heavily for data most of which would never be looked at. A checked
medicine's page is fetched once and cached to disk indefinitely.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CACHE_DIR = Path(__file__).parent.parent / "data" / "indications_cache"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
REQUEST_TIMEOUT_SECONDS = 15

SECTION_IDS = {
    "indications": "indications",
    "contraindications": "contraindications",
    "side_effects": "side_effects",
    "pregnancy_cat": "pregnancy_lactation",
}


@dataclass
class MedicineReference:
    brand_id: str
    source_url: str
    indications: str | None
    contraindications: str | None
    side_effects: str | None
    pregnancy_lactation: str | None
    fetched_from_cache: bool


def _cache_path(brand_id: str) -> Path:
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", brand_id)
    return CACHE_DIR / f"{safe_id}.json"


def _parse_sections(html: str) -> dict[str, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str | None] = {v: None for v in SECTION_IDS.values()}

    for section_id, key in SECTION_IDS.items():
        header_div = soup.find("div", id=section_id)
        if not header_div:
            continue
        body = header_div.find_next_sibling("div", class_="ac-body")
        if body:
            text = body.get_text(strip=True)
            result[key] = text or None

    return result


def get_reference(brand_id: str, source_url: str, force_refresh: bool = False) -> MedicineReference:
    cache_file = _cache_path(brand_id)

    if not force_refresh and cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        data["fetched_from_cache"] = True
        return MedicineReference(**data)

    try:
        resp = requests.get(source_url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not fetch medicine reference page: {exc}") from exc

    sections = _parse_sections(resp.text)
    reference = MedicineReference(
        brand_id=brand_id,
        source_url=source_url,
        fetched_from_cache=False,
        **sections,
    )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(asdict(reference), ensure_ascii=False, indent=2), encoding="utf-8")

    return reference
