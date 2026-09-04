# Deployment (Render, free tier)

This app is deployed on [Render](https://render.com)'s free web service
tier. These are the exact steps used, kept here so the deploy is
reproducible without re-deriving it.

## Why Render free tier

- No card required, genuinely free (not a trial)
- Deploys straight from a GitHub repo
- Free HTTPS — required for mobile camera capture (`capture="environment"`)
  and for the PWA "Add to Home Screen" flow
- Tradeoff accepted: the free plan sleeps after ~15 minutes idle, so the
  first request after a quiet period takes ~30-50s to wake up. Fine for a
  prototype/portfolio project; revisit if this needs to feel instant for
  daily real users (see "Upgrading" below).

## One-time setup

### 1. Push the repo to GitHub

```bash
git remote add origin https://github.com/<your-username>/fake-medicine-identifier.git
git branch -M main
git push -u origin main
```

### 2. Create the Render web service

1. [render.com](https://render.com) → sign up (GitHub login) → **New +** → **Web Service**
2. Connect the `fake-medicine-identifier` GitHub repo
3. Render's manual web-service flow does **not** auto-read `render.yaml`
   (that only happens via the Blueprint flow) — fill these in by hand:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --app-dir backend --host 0.0.0.0 --port $PORT`
   - **Compute plan**: select **$0 / month (Free)** — it defaults to a
     paid plan, easy to miss
4. **Environment Variables** → **Add Environment Variable**:
   - Key: `GEMINI_API_KEY`
   - Value: a Gemini API key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
     (see the free-tier note in the main README — a key created via
     "Default Gemini Project" works with `google-genai`'s simple
     `api_key=` auth)
5. **Deploy web service**

Render then builds and deploys automatically on every push to `main`.

## What `render.yaml` is for

`render.yaml` at the repo root documents the same config as code (Render
calls this a **Blueprint**). It's not currently wired to this service
since the service was created via the manual flow above, but it means the
same setup can be recreated automatically in the future via **New +** →
**Blueprint** instead of re-entering these settings by hand — useful for
a second environment, or if this service ever needs to be recreated.

## Verifying a deploy

Once live, check:

```
GET https://<your-service>.onrender.com/api/health
```

Should return:
```json
{"status": "ok", "drug_records_loaded": 25387, "api_key_configured": true}
```

If `api_key_configured` is `false`, the `GEMINI_API_KEY` environment
variable isn't set correctly in Render's dashboard (Settings → Environment).

If `drug_records_loaded` is `0`, `data/drugs.csv` didn't make it into the
deployed build — confirm it's committed to git (`git ls-files data/`) and
not caught by `.gitignore`.

## Rotating the Gemini API key

If the key has ever been shared in a chat, screenshot, or anywhere outside
your own machine and Render's dashboard, rotate it:

1. [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → delete the old key → create a new one
2. Update it locally: `setx GEMINI_API_KEY "<new-key>"` (reopen terminal after)
3. Update it on Render: **Dashboard → your service → Environment → edit `GEMINI_API_KEY`** → save
   (Render redeploys automatically when an env var changes)

## Upgrading later

If free-tier sleep becomes a real problem (e.g. real daily users, not just
testing):
- Render's paid tiers remove the sleep behavior (starts around $7/month)
- Alternatively, a free external uptime pinger (e.g. UptimeRobot hitting
  `/api/health` every 10 minutes) keeps the service awake within the free
  tier, though this uses up free-tier hours faster and isn't guaranteed
  to be reliable
