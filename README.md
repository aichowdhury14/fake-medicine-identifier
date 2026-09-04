# Fake Medicine Identifier (Bangladesh)

Photo in, safety answer out: snap a picture of a medicine strip/box, and the
app cross-checks the brand name, strength, and manufacturer against
Bangladesh's public medicine directory, and checks whether the printed
expiry date has already passed. For a matched medicine, it can also show
general reference info — what it's used for, contraindications, side
effects, and pregnancy/lactation warnings.

## Scope boundary on medical info

The "what is this medicine for" feature shows the **standard, textbook
use-case of a known drug** (e.g. "Paracetamol: pain relief, fever
reduction") as reference information, sourced from MedEx's own
pharmacist-written drug reference pages. It deliberately does **not**
accept a symptom and suggest a medicine — going from "I have a headache"
to "take X" is diagnosis-adjacent and meaningfully riskier than identity
verification, so it's out of scope for this tool. Every response carries
a "consult a doctor or pharmacist" disclaimer for the same reason.

## Honest scope

This checks packaging against **MedEx.com.bd**, Bangladesh's largest public
pharmacy directory (30,000+ brands) — not against DGDA's official drug
registration database. DGDA (the actual regulator) has no public API and its
own site had an invalid SSL certificate at the time this was built, so a
live official lookup wasn't possible.

What this means in practice:
- **It can catch:** misspelled/fake brand names, a real brand name printed
  under the wrong manufacturer, products not listed anywhere in the public
  directory, and expired stock.
- **It cannot catch:** a sophisticated counterfeit that copies a genuine,
  currently-registered product's packaging exactly (name, strength, and
  manufacturer all correct) — that requires physical/chemical verification,
  which no photo-based tool can do.

The app's verdict wording reflects this directly ("found in directory" /
"not found in directory"), not "verified genuine."

## Architecture

```
Photo upload (frontend/index.html)
        │
        ▼
FastAPI backend (backend/main.py)
        │
        ├─▶ vision.py       — Gemini vision API: OCR + structured extraction
        │                     (brand, strength, manufacturer, batch, expiry)
        │
        ├─▶ drug_db.py      — in-memory lookup against data/drugs.csv
        │                     (exact match → fuzzy match → not found)
        │
        ├─▶ verdict.py      — combines extraction + lookup + expiry date
        │                     into one plain-language verdict
        │
        └─▶ indications.py  — on-demand fetch + cache of a matched
                               medicine's Indications/Contraindications/
                               Side Effects/Pregnancy info from its MedEx
                               detail page (GET /api/reference/{brand_id})
```

Data source: `scraper/scrape_medex.py` scrapes MedEx's brand directory
(846 pages as of Sept 2026) into `data/drugs.csv`. Per-medicine reference
info (`indications.py`) is fetched live per medicine, only when a user
asks "what is this for," and cached to `data/indications_cache/` — not
bulk-scraped, to avoid hammering MedEx's server for data most of which
would never be viewed.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Get a **free** Gemini API key (no card required) at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey), then set it:

```bash
setx GEMINI_API_KEY "AIza..."      # Windows, permanent
# or for the current shell only:
$env:GEMINI_API_KEY = "AIza..."    # PowerShell
```

Free tier: low triple-digit requests/day, single-digit-to-low-teens/minute
on `gemini-flash-latest` — plenty for prototyping (exact limits vary by
model and change over time; check [ai.google.dev/pricing](https://ai.google.dev/pricing)
for current numbers). Note that on the free tier Google may use submitted
data to improve their products (not the case on paid tiers), which is
worth knowing before uploading real photos at scale.

`vision.py` uses the `gemini-flash-latest` alias rather than a pinned
version like `gemini-2.5-flash` — pinned model versions can get
deprecated for new API keys with little warning, which the alias avoids.

## Build the drug directory (one-time, re-run periodically)

```bash
cd scraper
python scrape_medex.py                    # scrapes all ~846 pages, ~10 min
python scrape_medex.py --pages 50          # or just the first 50 pages for a quick test
```

Output goes to `data/drugs.csv`. The scraper is resumable — re-running with
the same `--out` path skips duplicate brand IDs already written.

## Run the app

```bash
cd backend
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000 — upload or photograph a medicine strip, and get
a verdict.

## API

`POST /api/check` — multipart form, field `photo` (JPEG/PNG/WebP, max 10MB).

Returns:
```json
{
  "status": "found_in_directory" | "name_found_manufacturer_mismatch" |
            "similar_name_only" | "not_found_in_directory" |
            "expired" | "unreadable",
  "headline": "...",
  "explanation": "...",
  "action": "...",
  "matched_candidates": [...],
  "extracted": { "brand_name": "...", "strength": "...", ... }
}
```

`GET /api/reference/{brand_id}` — general reference info for one matched
medicine (`brand_id` comes from a `/api/check` response's
`matched_candidates`).

Returns:
```json
{
  "brand_id": "...", "source_url": "...",
  "indications": "..." | null,
  "contraindications": "..." | null,
  "side_effects": "..." | null,
  "pregnancy_lactation": "..." | null,
  "fetched_from_cache": true | false,
  "disclaimer": "This is general reference information..."
}
```

`GET /api/health` — dataset size + whether the API key is configured.

## Known limitations / next steps

- **Data freshness**: MedEx is a third-party directory, not DGDA — a
  genuine future version should integrate DGDA data once they publish a
  reliable public source (or via an official data-sharing request).
- **No batch/expiry cross-check against a manufacturer's real records** —
  expiry logic only checks if the printed date has passed, not whether the
  batch number is genuine.
- **English + Bangla mixed packaging**: vision extraction handles both via
  Gemini's multilingual OCR, but hasn't been stress-tested against heavily
  stylized Bangla fonts on damaged packaging.
- **Reference info cache never expires**: `data/indications_cache/` files
  are written once and reused forever. Fine for a prototype (drug
  indications rarely change), but a production version should add a TTL
  or manual refresh path in case MedEx corrects an entry.
- **WhatsApp/Messenger bot front-end** was discussed as the ideal
  distribution channel for rural reach beyond this web prototype — the
  `/api/check` endpoint is already bot-ready (same multipart photo upload),
  so a bot integration is a thin layer on top of the existing backend.
