"""
Combines vision extraction + directory lookup + expiry parsing into a
single, honestly-worded verdict.

Scope boundary (must stay visible to the user): this checks a photo
against a public pharmacy directory (MedEx), not against DGDA's official
registration database. It can catch obviously fake/misspelled/unknown
products and expired stock. It cannot certify that a product is
genuinely, officially DGDA-registered, and it cannot detect a
sophisticated counterfeit that copies a real, valid brand's packaging
exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from drug_db import DrugRecord
from vision import ExtractedMedicine

VERDICT_FOUND = "found_in_directory"
VERDICT_NAME_MISMATCH = "name_found_manufacturer_mismatch"
VERDICT_SIMILAR = "similar_name_only"
VERDICT_NOT_FOUND = "not_found_in_directory"
VERDICT_EXPIRED = "expired"
VERDICT_UNREADABLE = "unreadable"


@dataclass
class Verdict:
    status: str
    headline: str
    explanation: str
    action: str
    matched_candidates: list[dict] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _try_parse_expiry(expiry_text: str | None) -> date | None:
    if not expiry_text:
        return None
    text = expiry_text.strip().lower()

    m = re.match(r"(\d{1,2})[/\-.](\d{4})", text)
    if m:
        month, year = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return date(year, month, 1)

    m = re.match(r"([a-z]{3,})[\s/\-.]+(\d{4})", text)
    if m:
        month = _MONTHS.get(m.group(1)[:3])
        year = int(m.group(2))
        if month:
            return date(year, month, 1)

    m = re.match(r"(\d{4})[/\-.](\d{1,2})", text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            return date(year, month, 1)

    return None


def _record_to_dict(r: DrugRecord) -> dict:
    return {
        "brand_id": r.brand_id,
        "brand_name": r.brand_name,
        "strength": r.strength,
        "generic_name": r.generic_name,
        "manufacturer": r.manufacturer,
        "dosage_form": r.dosage_form,
        "source_url": r.source_url,
    }


def build_verdict(extracted: ExtractedMedicine, lookup_result: dict) -> Verdict:
    extracted_dict = {
        "brand_name": extracted.brand_name,
        "strength": extracted.strength,
        "generic_name": extracted.generic_name,
        "manufacturer": extracted.manufacturer,
        "batch_number": extracted.batch_number,
        "expiry_date": extracted.expiry_date,
        "manufacture_date": extracted.manufacture_date,
        "legibility": extracted.legibility,
        "notes": extracted.notes,
    }

    if not extracted.brand_name or extracted.legibility == "poor":
        return Verdict(
            status=VERDICT_UNREADABLE,
            headline="Could not read the packaging clearly",
            explanation="The photo doesn't show a clear enough brand name to check. "
                        + (extracted.notes or ""),
            action="Retake the photo in good light with the brand name and strength "
                   "clearly visible, then try again.",
            extracted=extracted_dict,
        )

    expiry = _try_parse_expiry(extracted.expiry_date)
    if expiry and expiry < date.today():
        return Verdict(
            status=VERDICT_EXPIRED,
            headline=f"This medicine appears EXPIRED ({extracted.expiry_date})",
            explanation="The expiry date printed on the packaging has already passed. "
                        "Do not use this medicine regardless of directory status.",
            action="Do not consume. Return it to the pharmacy and report the seller.",
            extracted=extracted_dict,
        )

    status = lookup_result["status"]
    candidates = [_record_to_dict(r) for r in lookup_result["candidates"]]

    if status == "exact_match":
        return Verdict(
            status=VERDICT_FOUND,
            headline=f'"{extracted.brand_name}" is a known brand in the public directory',
            explanation="The brand name and manufacturer match an entry in Bangladesh's "
                        "public medicine directory (MedEx). This is a directory cross-check, "
                        "not official DGDA registration verification — packaging can still be "
                        "counterfeited to copy a real, valid brand.",
            action="Directory check passed. Still buy only from licensed pharmacies and check "
                   "the expiry date yourself.",
            matched_candidates=candidates,
            extracted=extracted_dict,
        )

    if status == "name_match_manufacturer_mismatch":
        return Verdict(
            status=VERDICT_NAME_MISMATCH,
            headline=f'"{extracted.brand_name}" exists, but not from "{extracted.manufacturer}"',
            explanation="A brand with this name exists in the directory, but under a "
                        "different manufacturer than what's printed on this packaging. "
                        "This is a common counterfeit pattern — copying a real brand name "
                        "onto packaging from an unauthorized source.",
            action="Do not use. Verify with the pharmacist or manufacturer directly before consuming.",
            matched_candidates=candidates,
            extracted=extracted_dict,
        )

    if status == "similar_name_found":
        names = ", ".join(sorted({c["brand_name"] for c in candidates}))
        return Verdict(
            status=VERDICT_SIMILAR,
            headline=f'"{extracted.brand_name}" not found exactly — did you mean: {names}?',
            explanation="No exact match, but similarly-spelled brands exist in the directory. "
                        "This could be a photo misread, OR a counterfeit using a deliberately "
                        "similar name to a real product.",
            action="Double-check the spelling on the packaging against the suggestions above. "
                   "If it genuinely doesn't match, treat it as unverified.",
            matched_candidates=candidates,
            extracted=extracted_dict,
        )

    return Verdict(
        status=VERDICT_NOT_FOUND,
        headline=f'"{extracted.brand_name}" was not found in the public directory',
        explanation="No matching or similar brand name exists in Bangladesh's public medicine "
                    "directory. This could mean it's a very new product not yet listed, "
                    "an herbal/unregulated product, or a fake/unregistered product.",
        action="Do not assume it's safe. Verify with a licensed pharmacist before use.",
        extracted=extracted_dict,
    )
