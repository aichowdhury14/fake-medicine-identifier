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

Free tier notes (Gemini API, confirmed directly against a live key on
2026-09-05): the daily cap has tightened significantly since this was
first built — currently 20 requests/day for the resolved flash model
(GenerateRequestsPerDayPerProjectPerModel-FreeTier), not the ~250/day
figure that was true earlier in the year. On the free tier Google may
also use submitted data to improve their products (not the case on paid
tiers).

Two distinct failure modes need different handling:
- 503 UNAVAILABLE ("high demand") is transient server load — retrying
  after a short delay usually works.
- 429 RESOURCE_EXHAUSTED (daily/per-minute quota hit) will NOT resolve
  by retrying within the same request — the API itself reports a
  retryDelay of tens of seconds to (for the daily cap) potentially
  hours. Retrying this immediately just burns wall-clock time for a
  guaranteed-identical failure, so it's treated as non-retryable here;
  the caller (main.py) uses the local daily counter in quota.py to avoid
  even attempting a call once the day's budget is known to be spent.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from google import genai
from google.genai import types
from google.genai.errors import ClientError, ServerError

MODEL = "gemini-flash-latest"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2


class QuotaExceededError(RuntimeError):
    """Raised when Gemini reports the daily/per-minute quota is exhausted (429). Not retryable within this request."""


class ServiceUnavailableError(RuntimeError):
    """Raised when Gemini is transiently overloaded (503) and stays that way through all retries."""

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
        except ClientError as exc:
            if exc.code == 429:
                raise QuotaExceededError(
                    "Gemini's free-tier request quota is exhausted for now."
                ) from exc
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)

    if response is None:
        if isinstance(last_error, ServerError):
            raise ServiceUnavailableError(
                f"Gemini stayed overloaded through all {MAX_RETRIES} attempts."
            ) from last_error
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
