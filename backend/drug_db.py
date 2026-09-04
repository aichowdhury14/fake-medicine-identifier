"""
Loads the scraped MedEx drug directory into memory and provides fuzzy
lookup against it.

This is a directory cross-check, not an official DGDA registration check
(see scraper/scrape_medex.py docstring for why). Keep result wording
literal: "found in directory" / "not found in directory."
"""

from __future__ import annotations

import csv
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

DATA_PATH = Path(__file__).parent.parent / "data" / "drugs.csv"


@dataclass
class DrugRecord:
    brand_id: str
    brand_name: str
    strength: str
    generic_name: str
    manufacturer: str
    dosage_form: str
    source_url: str


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


class DrugDatabase:
    def __init__(self, csv_path: Path = DATA_PATH):
        self.records: list[DrugRecord] = []
        self._by_normalized_name: dict[str, list[DrugRecord]] = {}
        self._all_normalized_names: list[str] = []
        self._by_id: dict[str, DrugRecord] = {}
        self.load(csv_path)

    def load(self, csv_path: Path) -> None:
        if not csv_path.exists():
            self.records = []
            return

        with csv_path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                record = DrugRecord(
                    brand_id=row.get("brand_id", ""),
                    brand_name=row.get("brand_name", ""),
                    strength=row.get("strength", ""),
                    generic_name=row.get("generic_name", ""),
                    manufacturer=row.get("manufacturer", ""),
                    dosage_form=row.get("dosage_form", ""),
                    source_url=row.get("source_url", ""),
                )
                self.records.append(record)
                key = _normalize(record.brand_name)
                self._by_normalized_name.setdefault(key, []).append(record)
                if record.brand_id:
                    self._by_id[record.brand_id] = record

        self._all_normalized_names = list(self._by_normalized_name.keys())

    def __len__(self) -> int:
        return len(self.records)

    def get_by_id(self, brand_id: str) -> DrugRecord | None:
        return self._by_id.get(brand_id)

    def exact_matches(self, brand_name: str) -> list[DrugRecord]:
        return self._by_normalized_name.get(_normalize(brand_name), [])

    def fuzzy_matches(self, brand_name: str, limit: int = 5, cutoff: float = 0.72) -> list[DrugRecord]:
        query = _normalize(brand_name)
        if not query:
            return []
        close_keys = difflib.get_close_matches(query, self._all_normalized_names, n=limit, cutoff=cutoff)
        results = []
        for key in close_keys:
            results.extend(self._by_normalized_name[key])
        return results[:limit]

    def lookup(self, brand_name: str, manufacturer: str | None = None) -> dict:
        exact = self.exact_matches(brand_name)
        if exact:
            if manufacturer:
                mfr_norm = _normalize(manufacturer)
                mfr_match = [r for r in exact if mfr_norm in _normalize(r.manufacturer)
                             or _normalize(r.manufacturer) in mfr_norm]
                if mfr_match:
                    return {"status": "exact_match", "candidates": mfr_match}
                return {"status": "name_match_manufacturer_mismatch", "candidates": exact}
            return {"status": "exact_match", "candidates": exact}

        fuzzy = self.fuzzy_matches(brand_name)
        if fuzzy:
            return {"status": "similar_name_found", "candidates": fuzzy}

        return {"status": "not_found", "candidates": []}


_db_instance: DrugDatabase | None = None


def get_db() -> DrugDatabase:
    global _db_instance
    if _db_instance is None:
        _db_instance = DrugDatabase()
    return _db_instance
