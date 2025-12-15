# Pro-Downloader

A simple YouTube downloader with a Next.js (frontend) + FastAPI (backend) stack.

## Features

- Video mode and Audio-only mode
- Hybrid delivery:
  - **≤ 720p**: tries **Direct Download** when a progressive (audio+video in one file) URL is available
  - Otherwise (and **> 720p**): **Server Processing** with live progress (video → audio → merge)
- Storage safety:
  - Files are streamed to the client and cleaned up after streaming
  - A background sweeper periodically deletes old files from `backend/temp_downloads/`

## Project Structure

- `frontend/` — Next.js app (UI)
- `backend/` — FastAPI app (download/progress API)

## Requirements

- Node.js (LTS recommended)
- Python 3.10+
- FFmpeg (this project uses `static-ffmpeg`)

## Run Locally (Windows)

### 1) Backend (FastAPI)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 8000
```

Backend runs at: http://127.0.0.1:8000

### 2) Frontend (Next.js)

Open a new terminal:

```powershell
cd frontend
npm install
npm run dev
```

Frontend runs at: http://localhost:3000

## Environment Variables (optional)

Backend temp cleanup settings:

- `TEMP_FILE_MAX_AGE_SECONDS` (default: `3600`)
- `TEMP_CLEANUP_INTERVAL_SECONDS` (default: `1800`)

Example:

```powershell
$env:TEMP_FILE_MAX_AGE_SECONDS="1800"   # 30 minutes
$env:TEMP_CLEANUP_INTERVAL_SECONDS="600" # 10 minutes
python -m uvicorn main:app --reload --port 8000
```

## Notes

- Direct download is only possible when YouTube provides a **progressive** file (audio+video together). Many videos provide **separate** audio/video streams even at ≤ 720p, so the app automatically falls back to server processing for a reliable MP4/MP3 output.

## Fix: "Sign in to confirm you're not a bot"

Sometimes YouTube blocks anonymous downloads and requires **logged-in cookies**. This app supports cookies automatically.

- **Render (best / secure):** cookies ko git me push mat karo. Render me **Secret Env Var** me do, backend startup pe `backend/cookies.txt` khud bana lega.
- **Local dev (optional):** `backend/cookies.txt` bana ke cookies paste kar sakte ho (but commit mat karna).
- **Local dev (optional):** read cookies directly from your browser by setting:
  - `YTDLP_COOKIES_FROM_BROWSER=chrome` (or `firefox`, `edge`, etc)
  - You can also target a profile: `YTDLP_COOKIES_FROM_BROWSER=chrome:Profile 2`

Cookie options (backend):

- `YTDLP_COOKIES_FILE` (default: `backend/cookies.txt`)
- `YTDLP_COOKIES_FROM_BROWSER`

Render secret env vars (backend):

- `YTDLP_COOKIES_B64` (preferred)
- `YTDLP_COOKIES_TEXT` (only if your platform supports multiline secrets)

### Render pe exact steps (simple)

1) Chrome/Firefox se YouTube cookies export karo (Netscape `cookies.txt`).
2) Repo root pe ye command chalao (Windows):

```powershell
./tools/encode-cookies.ps1 -CookiesPath backend/cookies.txt
```

3) Render Dashboard → Service → Environment → **Add Secret**:
  - Key: `YTDLP_COOKIES_B64`
  - Value: (script ka output)
4) Redeploy.

Note: cookies expire ho sakti hain—phir se error aaye to repeat.
