"""
Extracts medicine identity fields from a photo using Google Gemini's
free-tier vision API.

One call does OCR + structured extraction: given a photo of a medicine
strip/box, return brand name, strength, manufacturer, and any visible
batch/expiry text, plus a confidence note on legibility.

Model choice: "gemini-flash-latest" is an alias Google keeps pointed at
their current recommended flash model, rather than a pinned version like
"gemini-2.5-flash" — pinned versions get deprecated for new API keys
without much warning (confirmed directly against a live key), so the
alias is more durable for a prototype that shouldn't need code changes
every few months.

Free tier notes (Gemini API, as of this writing): no card required, low
triple-digit requests/day and single-digit-to-low-teens requests/minute,
varies by exact model. On the free tier Google may use submitted data to
improve their products (this does not apply on paid tiers) — fine for a
prototype, but worth knowing before sending real patient/medicine photos
at scale. The API can also return transient 503 "high demand" errors
under load, unrelated to your key or code — worth a retry, not a bug.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from google import genai
from google.genai import types

MODEL = "gemini-flash-latest"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

EXTRACTION_PROMPT = """You are looking at a photo of a medicine strip, blister pack, or box sold in Bangladesh.

Extract exactly what is printed on the packaging. Do not guess or invent values you cannot see clearly — use null for anything illegible or absent.

Respond with ONLY a JSON object, no other text, no markdown fences, in this exact shape:
{
  "brand_name": string or null,
  "strength": string or null,
  "generic_name": string or null,
  "manufacturer": string or null,
  "batch_number": string or null,
  "expiry_date": string or null,
  "manufacture_date": string or null,
  "legibility": "clear" | "partial" | "poor",
  "notes": string
}"""


@dataclass
class ExtractedMedicine:
    brand_name: str | None
    strength: str | None
    generic_name: str | None
    manufacturer: str | None
    batch_number: str | None
    expiry_date: str | None
    manufacture_date: str | None
    legibility: str
    notes: str


def _mime_type_for(filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "jpeg"
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(ext, "image/jpeg")


def extract_medicine_info(image_bytes: bytes, filename: str = "photo.jpg") -> ExtractedMedicine:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set")

    client = genai.Client(api_key=api_key)

    last_error: Exception | None = None
    response = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=_mime_type_for(filename)),
                    EXTRACTION_PROMPT,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            break
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)

    if response is None:
        raise RuntimeError(f"Gemini API request failed after {MAX_RETRIES} attempts: {last_error}")

    raw_text = (response.text or "").strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse model response as JSON: {raw_text[:300]}") from exc

    return ExtractedMedicine(
        brand_name=data.get("brand_name"),
        strength=data.get("strength"),
        generic_name=data.get("generic_name"),
        manufacturer=data.get("manufacturer"),
        batch_number=data.get("batch_number"),
        expiry_date=data.get("expiry_date"),
        manufacture_date=data.get("manufacture_date"),
        legibility=data.get("legibility", "poor"),
        notes=data.get("notes", ""),
    )
