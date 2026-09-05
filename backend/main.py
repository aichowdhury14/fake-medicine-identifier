from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import quota
from drug_db import get_db
from indications import get_reference
from verdict import build_verdict
from vision import (
    QuotaExceededError,
    ServiceUnavailableError,
    extract_medicine_info,
    extract_prescription_text,
)

MAX_BATCH_SIZE = 4

app = FastAPI(title="Fake Medicine Identifier (Bangladesh)")

# Frontend is served from the same origin as the API (see the mount below),
# so no cross-origin requests are expected. CORS stays permissive only for
# local development convenience (e.g. opening frontend/index.html directly).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Only the handful of user-facing error strings that can't go through the
# frontend's own STRINGS dictionary, because they originate server-side
# and are shown before any verdict JSON reaches the page's normal i18n path.
QUOTA_LOCAL_EXHAUSTED_MSG = {
    "en": "This tool runs on a free daily quota that's been used up for today "
          "(Bangladesh time resets at 6am). Please try again after it resets.",
    "bn": "এই টুলটি আজকের জন্য একটি বিনামূল্যে দৈনিক সীমার মধ্যে চলে, যা শেষ হয়ে গেছে "
          "(বাংলাদেশ সময় সকাল ৬টায় পুনরায় চালু হয়)। অনুগ্রহ করে পরে আবার চেষ্টা করুন।",
}
QUOTA_REAL_EXHAUSTED_MSG = {
    "en": "This tool has hit Google's real daily limit for today, which can happen "
          "even if the counter on this page still showed checks left. Please try "
          "again after it resets (around 6am Bangladesh time).",
    "bn": "এই টুলটি আজকের জন্য Google-এর প্রকৃত দৈনিক সীমায় পৌঁছে গেছে, যা এই পাতার "
          "কাউন্টারে এখনও যাচাই বাকি দেখালেও ঘটতে পারে। অনুগ্রহ করে সীমা পুনরায় চালু হওয়ার "
          "পর আবার চেষ্টা করুন (বাংলাদেশ সময় প্রায় সকাল ৬টা)।",
}
SERVICE_BUSY_MSG = {
    "en": "The AI service is briefly overloaded with requests right now. This "
          "usually clears up within a minute or two — please try again shortly.",
    "bn": "AI সেবাটি এই মুহূর্তে সাময়িকভাবে অতিরিক্ত ব্যস্ত। এটি সাধারণত এক-দুই মিনিটের "
          "মধ্যে ঠিক হয়ে যায় — অনুগ্রহ করে একটু পরে আবার চেষ্টা করুন।",
}
BATCH_TOO_LARGE_MSG = {
    "en": f"You can check up to {MAX_BATCH_SIZE} medicines at once, to keep the "
          f"free daily quota available for everyone. Please split this into smaller batches.",
    "bn": f"সবার জন্য বিনামূল্যে দৈনিক সীমা রাখতে, আপনি একসাথে সর্বোচ্চ {MAX_BATCH_SIZE}টি "
          f"ওষুধ যাচাই করতে পারবেন। অনুগ্রহ করে ছোট ছোট ভাগে যাচাই করুন।",
}


def _localized(messages: dict[str, str], lang: str) -> str:
    return messages.get(lang, messages["en"])


@app.get("/api/health")
def health():
    db = get_db()
    q = quota.get_status()
    return {
        "status": "ok",
        "drug_records_loaded": len(db),
        "api_key_configured": bool(os.environ.get("GEMINI_API_KEY")),
        "checks_remaining_today": q.remaining,
        "daily_limit": q.limit,
    }


async def _validate_upload(photo: UploadFile) -> bytes:
    if photo.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(400, f"Unsupported file type: {photo.content_type}. Use JPEG, PNG, or WebP.")

    image_bytes = await photo.read()
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Image too large (max 10MB).")
    if len(image_bytes) == 0:
        raise HTTPException(400, "Empty file.")
    return image_bytes


def _check_one(image_bytes: bytes, filename: str, lang: str) -> dict:
    """
    Runs the full identify-and-verify pipeline for one photo: quota check,
    Gemini extraction, directory lookup, verdict construction. Raises
    HTTPException on quota/service failures exactly like the single-check
    endpoint, so both /api/check and /api/check-batch share one error path.
    """
    q = quota.get_status()
    if q.exhausted:
        raise HTTPException(429, _localized(QUOTA_LOCAL_EXHAUSTED_MSG, lang))

    try:
        extracted = extract_medicine_info(image_bytes, filename)
    except QuotaExceededError:
        # Google's own account-level quota is exhausted — a different, harder limit
        # than our local per-instance counter below, and can be hit even while our
        # counter still shows room (e.g. other testing/traffic used the real quota).
        # Snap our local counter to exhausted too so the badge stops overpromising
        # availability until the shared daily quota actually resets.
        quota.mark_exhausted()
        raise HTTPException(429, _localized(QUOTA_REAL_EXHAUSTED_MSG, lang))
    except ServiceUnavailableError:
        raise HTTPException(503, _localized(SERVICE_BUSY_MSG, lang))
    except RuntimeError as exc:
        raise HTTPException(502, f"Could not analyze image: {exc}")

    quota.record_attempt()

    db = get_db()
    lookup_result = db.lookup(extracted.brand_name or "", extracted.manufacturer)
    verdict = build_verdict(extracted, lookup_result)

    return asdict(verdict)


@app.post("/api/check")
async def check_medicine(photo: UploadFile = File(...), lang: str = Query("en")):
    image_bytes = await _validate_upload(photo)
    return _check_one(image_bytes, photo.filename or "photo.jpg", lang)


@app.post("/api/check-batch")
async def check_medicines_batch(photos: list[UploadFile] = File(...), lang: str = Query("en")):
    """
    Checks multiple medicine packaging photos in one request (e.g. an entire
    home medicine cabinet). Capped at MAX_BATCH_SIZE to prevent one session
    from consuming a large share of the shared free daily quota (see
    quota.py) in a single visit.

    Each photo is processed independently: one failing (unreadable photo,
    quota hit mid-batch, etc.) does not abort the rest — every item in the
    response corresponds positionally to the uploaded photo, carrying
    either a verdict or an error, so the caller can render partial results.
    """
    if len(photos) > MAX_BATCH_SIZE:
        raise HTTPException(400, _localized(BATCH_TOO_LARGE_MSG, lang))
    if len(photos) == 0:
        raise HTTPException(400, "No photos provided.")

    results = []
    for photo in photos:
        try:
            image_bytes = await _validate_upload(photo)
            verdict = _check_one(image_bytes, photo.filename or "photo.jpg", lang)
            results.append({"filename": photo.filename, "ok": True, "verdict": verdict})
        except HTTPException as exc:
            results.append({"filename": photo.filename, "ok": False, "error": exc.detail, "status_code": exc.status_code})

    return {"results": results}


PRESCRIPTION_DISCLAIMER = {
    "en": "This only transcribes text from the photo and checks names against the public "
          "medicine directory — it does not interpret dosage, verify the prescription is "
          "correct or safe, or give medical advice. Always follow your doctor's actual "
          "instructions, and ask your pharmacist if anything here looks wrong.",
    "bn": "এটি শুধুমাত্র ছবি থেকে লেখা তুলে ধরে এবং নামগুলো সরকারি ওষুধ তালিকার সাথে মিলিয়ে দেখে — "
          "এটি মাত্রা ব্যাখ্যা করে না, প্রেসক্রিপশন সঠিক বা নিরাপদ কিনা যাচাই করে না, বা কোনো "
          "চিকিৎসা পরামর্শ দেয় না। সবসময় আপনার ডাক্তারের প্রকৃত নির্দেশ অনুসরণ করুন, এবং কিছু ভুল "
          "মনে হলে ফার্মাসিস্টকে জিজ্ঞাসা করুন।",
}


@app.post("/api/check-prescription")
async def check_prescription(photo: UploadFile = File(...), lang: str = Query("en")):
    """
    Transcribes medicine names from a photographed prescription and does a
    light directory lookup per name, purely as a "does this look like a
    real, listed medicine" reference check — the same identity-checking
    idea as /api/check, applied to prescription text instead of packaging.

    Deliberately does not interpret dosage, explain what a medicine is for,
    or comment on whether the prescription is appropriate. That is
    diagnosis-adjacent territory this tool stays out of; see the
    disclaimer returned with every response.
    """
    image_bytes = await _validate_upload(photo)

    q = quota.get_status()
    if q.exhausted:
        raise HTTPException(429, _localized(QUOTA_LOCAL_EXHAUSTED_MSG, lang))

    try:
        prescription = extract_prescription_text(image_bytes, photo.filename or "prescription.jpg")
    except QuotaExceededError:
        quota.mark_exhausted()
        raise HTTPException(429, _localized(QUOTA_REAL_EXHAUSTED_MSG, lang))
    except ServiceUnavailableError:
        raise HTTPException(503, _localized(SERVICE_BUSY_MSG, lang))
    except RuntimeError as exc:
        raise HTTPException(502, f"Could not analyze image: {exc}")

    quota.record_attempt()

    db = get_db()
    lines = []
    for med in prescription.medicines:
        lookup_result = db.lookup_text(med.raw_text)
        candidates = lookup_result["candidates"][:3]
        lines.append({
            "raw_text": med.raw_text,
            "confidence": med.confidence,
            "directory_match": lookup_result["status"] in ("exact_match", "similar_name_found"),
            "matched_on": lookup_result.get("matched_on"),
            "candidates": [
                {
                    "brand_id": r.brand_id, "brand_name": r.brand_name, "strength": r.strength,
                    "generic_name": r.generic_name, "manufacturer": r.manufacturer,
                }
                for r in candidates
            ],
        })

    return {
        "lines": lines,
        "unreadable_lines": prescription.unreadable_lines,
        "notes": prescription.notes,
        "disclaimer": _localized(PRESCRIPTION_DISCLAIMER, lang),
    }


@app.get("/api/reference/{brand_id}")
def reference(brand_id: str):
    """
    General reference info (indications, contraindications, side effects,
    pregnancy/lactation warnings) for a specific matched medicine.

    This is standard textbook drug information, not personalized medical
    advice, and never accepts a symptom to suggest a medicine. Always
    consult a doctor or pharmacist before starting, stopping, or changing
    any medication.
    """
    db = get_db()
    record = db.get_by_id(brand_id)
    if record is None:
        raise HTTPException(404, "Unknown brand_id — this medicine wasn't found in the directory.")

    try:
        ref = get_reference(record.brand_id, record.source_url)
    except RuntimeError as exc:
        raise HTTPException(502, f"Could not fetch reference info: {exc}")

    result = asdict(ref)
    result["disclaimer"] = (
        "This is general reference information about the medicine, not personalized "
        "medical advice. Always consult a doctor or pharmacist before starting, "
        "stopping, or changing any medication."
    )
    return result


frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def serve_index():
    return FileResponse(os.path.join(frontend_dir, "index.html"))
