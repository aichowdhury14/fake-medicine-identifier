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


_LEADING_NUMBERING_RE = re.compile(r"^\s*\d+[.):]\s*")
_DOSAGE_NOISE_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(mg|mcg|ml|g|iu|%)\b|"   # strength: "500mg", "10 ml"
    r"\b\d[\d+\-]*\b|"                          # frequency shorthand: "1+1+1", "1-0-1"
    r"\b(tab|tabs|tablet|tablets|cap|caps|capsule|capsules|syrup|inj|injection|"
    r"od|bid|tid|qid|prn|stat|daily|morning|evening|night|before|after|meal)\b",
    re.IGNORECASE,
)
# A medicine name is essentially always the first thing on a prescription
# line; everything after the first 1-3 words is strength/frequency/duration/
# instruction free text with no fixed vocabulary ("x 5 days", "as needed for
# fever", "before meal") that a word-list regex can't realistically cover.
# So the first candidate is just the leading word(s), stopping at the first
# digit or a small set of connector words that clearly end the name.
_NAME_BOUNDARY_RE = re.compile(
    r"^([A-Za-z][A-Za-z\-]*(?:\s+[A-Za-z][A-Za-z\-]*)?)(?=\s+(?:\d|as\b|for\b|before\b|after\b|with\b|at\b)|\s*$)",
    re.IGNORECASE,
)


def _strip_leading_numbering(text: str) -> str:
    """Removes a list marker like "1." or "2)" that Gemini may include when transcribing a numbered prescription."""
    return _LEADING_NUMBERING_RE.sub("", text).strip()


def _strip_dosage_noise(text: str) -> str:
    """
    Prescription transcriptions often read like "Napa 500mg 1+1+1" — a
    medicine name followed by strength and frequency shorthand. Directory
    matching needs just the name, so this trims the common noise patterns
    before falling back to a first-token guess.
    """
    stripped = _DOSAGE_NOISE_RE.sub(" ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped


def _leading_name_guess(text: str) -> str:
    """
    Extracts just the probable medicine name from the start of a full
    transcribed line, e.g. "Paracetamol as needed for fever" -> "Paracetamol",
    "Napa 500mg 1+1+1 x 5 days" -> "Napa". Falls back to the first word if
    the boundary pattern doesn't match cleanly.
    """
    match = _NAME_BOUNDARY_RE.match(text)
    if match:
        return match.group(1).strip()
    first_word = text.split()[0] if text.split() else ""
    return first_word


class DrugDatabase:
    def __init__(self, csv_path: Path = DATA_PATH):
        self.records: list[DrugRecord] = []
        self._by_normalized_name: dict[str, list[DrugRecord]] = {}
        self._all_normalized_names: list[str] = []
        self._by_id: dict[str, DrugRecord] = {}
        self._by_normalized_generic: dict[str, list[DrugRecord]] = {}
        self._all_normalized_generics: list[str] = []
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
                if record.generic_name:
                    generic_key = _normalize(record.generic_name)
                    self._by_normalized_generic.setdefault(generic_key, []).append(record)

        self._all_normalized_names = list(self._by_normalized_name.keys())
        self._all_normalized_generics = list(self._by_normalized_generic.keys())

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

    def lookup_by_generic(self, generic_name: str, limit: int = 5) -> list[DrugRecord]:
        """Finds brands that share a given generic/active-ingredient name, exact then fuzzy."""
        key = _normalize(generic_name)
        exact = self._by_normalized_generic.get(key, [])
        if exact:
            return exact[:limit]

        close_keys = difflib.get_close_matches(key, self._all_normalized_generics, n=limit, cutoff=0.75)
        results = []
        for k in close_keys:
            results.extend(self._by_normalized_generic[k])
        return results[:limit]

    def lookup_text(self, text: str, limit: int = 5) -> dict:
        """
        Loosely-formatted lookup for prescription transcriptions, which name
        either a brand or a generic and are usually followed by free-text
        strength/frequency/duration/instructions with no fixed vocabulary
        (e.g. "Napa 500mg 1+1+1 x 5 days", "Paracetamol as needed for
        fever"). A word-list-based noise stripper can't realistically cover
        that open-ended tail, so the primary strategy is extracting just the
        leading word(s) — where the medicine name actually is — and falling
        back to noise-stripping and the raw text only if that fails.
        """
        text = _strip_leading_numbering(text)
        candidates_to_try = [
            _leading_name_guess(text),
            _strip_dosage_noise(text),
            text,
        ]

        for candidate_text in candidates_to_try:
            if not candidate_text:
                continue

            brand_result = self.lookup(candidate_text)
            if brand_result["status"] in ("exact_match", "similar_name_found"):
                return {"matched_on": "brand_name", **brand_result}

            generic_matches = self.lookup_by_generic(candidate_text, limit=limit)
            if generic_matches:
                return {"matched_on": "generic_name", "status": "similar_name_found", "candidates": generic_matches}

        return {"matched_on": None, "status": "not_found", "candidates": []}

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
