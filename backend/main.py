from __future__ import annotations

import os
from dataclasses import asdict

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import quota
from drug_db import get_db
from indications import get_reference
from verdict import build_verdict
from vision import QuotaExceededError, extract_medicine_info

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


@app.post("/api/check")
async def check_medicine(photo: UploadFile = File(...)):
    if photo.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(400, f"Unsupported file type: {photo.content_type}. Use JPEG, PNG, or WebP.")

    image_bytes = await photo.read()
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Image too large (max 10MB).")
    if len(image_bytes) == 0:
        raise HTTPException(400, "Empty file.")

    q = quota.get_status()
    if q.exhausted:
        raise HTTPException(
            429,
            "This tool runs on a free daily quota that's been used up for today "
            "(Bangladesh time resets at 6am). Please try again after it resets.",
        )

    quota.record_attempt()
    try:
        extracted = extract_medicine_info(image_bytes, photo.filename or "photo.jpg")
    except QuotaExceededError:
        raise HTTPException(
            429,
            "This tool runs on a free daily quota that's been used up for today. "
            "Please try again after it resets.",
        )
    except RuntimeError as exc:
        raise HTTPException(502, f"Could not analyze image: {exc}")

    db = get_db()
    lookup_result = db.lookup(extracted.brand_name or "", extracted.manufacturer)
    verdict = build_verdict(extracted, lookup_result)

    return asdict(verdict)


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
