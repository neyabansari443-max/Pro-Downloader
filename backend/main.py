import os
import uuid
import asyncio
import re
import threading
import time
import urllib.request # Keep-alive ke liye
from typing import Optional, Dict, Union, Any, cast
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
import yt_dlp
import static_ffmpeg

# Initialize static ffmpeg
static_ffmpeg.add_paths()

app = FastAPI()

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_downloads")
os.makedirs(TEMP_DIR, exist_ok=True)

DEFAULT_COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")


def ensure_cookies_file_from_env() -> None:
    """Materialize cookies.txt from environment variables (for Render).

    Supported:
      - YTDLP_COOKIES_B64: base64-encoded full Netscape cookies.txt content
      - YTDLP_COOKIES_TEXT: raw Netscape cookies.txt content

    If a non-empty cookies file already exists, this is a no-op.
    """
    cookies_b64 = (os.getenv("YTDLP_COOKIES_B64") or "").strip()
    cookies_text = os.getenv("YTDLP_COOKIES_TEXT")

    content: str | None = None
    if cookies_b64:
        try:
            import base64

            content = base64.b64decode(cookies_b64).decode("utf-8", errors="replace")
        except Exception:
            content = None
    elif cookies_text:
        content = cookies_text

    if not content or not content.strip():
        # No env cookies provided; keep any on-disk cookies as-is.
        return

    # Basic sanity: Netscape cookie files typically start with this header.
    # If the user provides a different format, yt-dlp will error out later.
    try:
        os.makedirs(os.path.dirname(DEFAULT_COOKIES_PATH), exist_ok=True)
        with open(DEFAULT_COOKIES_PATH, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        print("✅ Cookies file created from env")
    except Exception as e:
        print(f"⚠️ Failed to write cookies file from env: {e}")


def get_ytdlp_auth_opts() -> Dict[str, object]:
    """Return yt-dlp auth options.

    YouTube sometimes requires logged-in cookies ("confirm you're not a bot").
    This supports either a cookie file (recommended for server) or reading
    cookies from an installed browser (local dev only).

    Env vars:
      - YTDLP_COOKIES_FILE: path to Netscape cookies.txt
      - YTDLP_COOKIES_FROM_BROWSER: e.g. 'chrome', 'firefox', 'edge', or 'chrome:Profile 2'
    """
    opts: Dict[str, object] = {}

    cookies_from_browser = (os.getenv("YTDLP_COOKIES_FROM_BROWSER") or "").strip()
    cookies_file = (os.getenv("YTDLP_COOKIES_FILE") or "").strip() or DEFAULT_COOKIES_PATH

    if cookies_from_browser:
        # yt-dlp expects a tuple: (browser,) or (browser, profile)
        browser, sep, profile = cookies_from_browser.partition(":")
        browser = browser.strip()
        profile = profile.strip() if sep else ""
        if browser:
            opts["cookiesfrombrowser"] = (browser, profile) if profile else (browser,)
        return opts

    try:
        if cookies_file and os.path.exists(cookies_file) and os.path.getsize(cookies_file) > 0:
            opts["cookiefile"] = cookies_file
    except Exception:
        pass

    return opts


def get_ytdlp_common_opts() -> Dict[str, object]:
    """Common yt-dlp options used by both /info and /download.

    Notes:
    - Don't force a single YouTube player client by default; yt-dlp will pick
      working clients automatically as YouTube changes.
    - You can override player clients with YTDLP_YOUTUBE_PLAYER_CLIENTS.
    """
    opts: Dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        **get_ytdlp_auth_opts(),
    }

    # Allow overriding UA to match the browser used to export cookies.
    user_agent = (os.getenv("YTDLP_USER_AGENT") or "").strip()
    if not user_agent:
        user_agent = (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36"
        )
    opts["http_headers"] = {"User-Agent": user_agent}

    # Optional: override YouTube player clients (comma-separated)
    # Example: YTDLP_YOUTUBE_PLAYER_CLIENTS=web,android,ios
    clients_raw = (os.getenv("YTDLP_YOUTUBE_PLAYER_CLIENTS") or "").strip()
    if clients_raw:
        clients = [c.strip() for c in clients_raw.split(",") if c.strip()]
        if clients:
            opts["extractor_args"] = {"youtube": {"player_client": clients}}

    return opts


def humanize_yt_dlp_error(err: Exception) -> str:
    text = str(err).replace("ERROR:", "").strip()
    lowered = text.lower()

    if (
        "confirm you" in lowered
        and "not a bot" in lowered
    ) or "--cookies-from-browser" in lowered or "--cookies" in lowered:
        return (
            "YouTube is asking for verification. Provide logged-in cookies and try again. "
            "Options: (1) export cookies to backend/cookies.txt (Netscape format), "
            "or (2) set YTDLP_COOKIES_FROM_BROWSER=chrome (local dev)."
        )

    if "failed to extract any player response" in lowered:
        return (
            "YouTube extraction failed (no player response). This is usually caused by YouTube changes, "
            "blocked IP/rate-limit, or stale cookies. Try: refresh cookies (incognito YouTube session), "
            "redeploy to update yt-dlp, and if needed set YTDLP_YOUTUBE_PLAYER_CLIENTS=web,android,ios."
        )

    if "po token" in lowered:
        return (
            "YouTube may require a PO Token for this video/format. Try different player clients (e.g. web/android), "
            "or follow yt-dlp's PO Token guidance. Cookies alone may not be sufficient in some cases."
        )

    # Keep the old short message behavior as a fallback
    return text.split(";")[0].strip()

# Temp cleanup settings (safe defaults)
TEMP_FILE_MAX_AGE_SECONDS = int(os.getenv("TEMP_FILE_MAX_AGE_SECONDS", "3600"))  # 1 hour
TEMP_CLEANUP_INTERVAL_SECONDS = int(os.getenv("TEMP_CLEANUP_INTERVAL_SECONDS", "1800"))  # 30 min

# Render URL for Keep-Alive (Apna Render URL yahan confirm kar lena)
RENDER_APP_URL = os.getenv("RENDER_EXTERNAL_URL", "https://pro-downloader-8fx4.onrender.com")

# In-memory job store
jobs: Dict[str, Dict] = {}

YOUTUBE_REGEX = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})'

class VideoInfoRequest(BaseModel):
    url: str
    type: str = "video" # video or audio

    @validator('url')
    def validate_youtube_url(cls, v):
        if not re.match(YOUTUBE_REGEX, v):
            raise ValueError('Invalid YouTube URL specified')
        return v

class DownloadRequest(BaseModel):
    url: str
    height: Optional[int] = None
    type: str = "video"

# --- Keep Alive Logic (New Feature) ---
def keep_alive_loop():
    """Har 10 minute me server ko khud ping karega taaki wo sleep mode me na jaye"""
    while True:
        time.sleep(600) # 10 Minutes wait
        try:
            print(f"⏰ Keep-Alive: Pinging {RENDER_APP_URL}/health ...")
            with urllib.request.urlopen(f"{RENDER_APP_URL}/health") as response:
                print(f"✅ Keep-Alive: Success (Status {response.getcode()})")
        except Exception as e:
            print(f"⚠️ Keep-Alive Error: {e}")

# --- Helper Functions (No Logic Removed) ---

def initialize_phases(dl_type: str) -> Dict[str, Dict[str, Union[str, float]]]:
    if dl_type == 'audio':
        return {
            'audio_download': {
                'label': 'Audio Download',
                'status': 'pending',
                'progress': 0.0,
            },
            'processing': {
                'label': 'Audio Processing',
                'status': 'pending',
                'progress': 0.0,
            }
        }
    return {
        'video_download': {
            'label': 'Video Download',
            'status': 'pending',
            'progress': 0.0,
        },
        'audio_download': {
            'label': 'Audio Download',
            'status': 'pending',
            'progress': 0.0,
        },
        'merging': {
            'label': 'Merging Streams',
            'status': 'pending',
            'progress': 0.0,
        }
    }

def update_overall_progress(job_id: str) -> None:
    job = jobs.get(job_id)
    if not job:
        return
    phases = job.get('phases', {})
    if not phases:
        return
    total = sum(phase.get('progress', 0.0) for phase in phases.values())
    job['progress'] = round(total / len(phases), 2)

def simulate_phase_progress(job_id: str, phase_key: str) -> None:
    def _runner():
        while True:
            job = jobs.get(job_id)
            if not job:
                break
            phase = job.get('phases', {}).get(phase_key)
            if not phase or phase.get('status') != 'processing':
                break
            current = phase.get('progress', 0.0)
            if current >= 95:
                break
            phase['progress'] = min(95.0, current + 5.0)
            update_overall_progress(job_id)
            time.sleep(0.6)
    threading.Thread(target=_runner, daemon=True).start()

async def delete_file_delayed(path: str, delay: int = 60):
    """Deletes a file after a delay to ensure serving is complete."""
    await asyncio.sleep(delay)
    try:
        if os.path.exists(path):
            os.remove(path)
            print(f"Cleaned up file: {path}")
    except Exception as e:
        print(f"Error cleaning up file {path}: {e}")

def stream_file_and_cleanup(job_id: str, path: str, chunk_size: int = 1024 * 1024):
    """Stream a file in chunks and delete it as soon as streaming ends."""
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"Cleaned up file after streaming: {path}")
        except Exception as e:
            print(f"Error cleaning up streamed file {path}: {e}")
        # Remove job metadata to avoid memory growth
        jobs.pop(job_id, None)

def cleanup_temp_dir(max_age_seconds: int = TEMP_FILE_MAX_AGE_SECONDS) -> int:
    """Delete files in TEMP_DIR older than max_age_seconds."""
    now = time.time()
    deleted = 0
    try:
        for name in os.listdir(TEMP_DIR):
            full_path = os.path.join(TEMP_DIR, name)
            try:
                if not os.path.isfile(full_path):
                    continue
                age = now - os.path.getmtime(full_path)
                if age < max_age_seconds:
                    continue
                os.remove(full_path)
                deleted += 1
            except Exception:
                continue
    except Exception:
        return deleted
    return deleted

async def temp_cleanup_loop() -> None:
    """Background task to keep TEMP_DIR from growing indefinitely."""
    while True:
        try:
            deleted = cleanup_temp_dir()
            if deleted:
                print(f"Temp sweeper deleted {deleted} old file(s) from {TEMP_DIR}")
        except Exception:
            pass
        await asyncio.sleep(TEMP_CLEANUP_INTERVAL_SECONDS)

# --- Startup Events ---
@app.on_event("startup")
async def startup_events() -> None:
    # 0. If cookies are provided as Render secrets, write them to cookies.txt
    ensure_cookies_file_from_env()

    # 1. Start Temp Cleaner
    asyncio.create_task(temp_cleanup_loop())
    
    # 2. Start Keep-Alive Pinger (Background Thread)
    # Ye thread server start hone ke baad background me chalta rahega
    threading.Thread(target=keep_alive_loop, daemon=True).start()
    print("✅ Keep-Alive System Started")

def ydl_progress_hook(d, job_id):
    """Update per-phase progress for the active job."""
    job = jobs.get(job_id)
    if not job:
        return

    info = d.get('info_dict') or {}
    phase_key = 'audio_download' if info.get('vcodec') in (None, 'none') else 'video_download'
    if job['type'] == 'audio':
        phase_key = 'audio_download'

    if d['status'] == 'downloading':
        try:
            percent_str = d.get('_percent_str', '0.0%')
            percent = float(re.sub(r'\x1b\[[0-9;]*m', '', percent_str).strip('%'))
            phase = job['phases'].get(phase_key)
            if phase:
                phase['status'] = 'downloading'
                phase['progress'] = percent
            job['status'] = 'processing'
            update_overall_progress(job_id)
        except Exception:
            pass
    elif d['status'] == 'finished':
        phase = job['phases'].get(phase_key)
        if phase:
            phase['status'] = 'completed'
            phase['progress'] = 100.0
        update_overall_progress(job_id)

def ydl_postprocessor_hook(d, job_id):
    job = jobs.get(job_id)
    if not job:
        return
    pp_name = d.get('postprocessor')
    status = d.get('status')
    if job['type'] == 'audio' and pp_name == 'FFmpegExtractAudio':
        phase_key = 'processing'
    elif job['type'] == 'video' and pp_name == 'FFmpegMerger':
        phase_key = 'merging'
    else:
        return

    phase = job['phases'].get(phase_key)
    if not phase:
        return

    if status == 'started':
        phase['status'] = 'processing'
        phase['progress'] = max(phase['progress'], 5.0)
        simulate_phase_progress(job_id, phase_key)
    elif status == 'finished':
        phase['status'] = 'completed'
        phase['progress'] = 100.0
    update_overall_progress(job_id)

# --- Routes ---

# Health Check Route (Keep-Alive ke liye zaroori)
@app.get("/health")
async def health_check():
    return {"status": "alive"}

@app.post("/info")
async def get_video_info(request: VideoInfoRequest):
    try:
        ydl_opts = get_ytdlp_common_opts()
        with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
            info = ydl.extract_info(request.url, download=False)
            
            formats_list = []
            
            if request.type == 'audio':
                formats_list.append({
                    'format_id': 'bestaudio',
                    'height': 0, 
                    'ext': 'mp3', 
                    'filesize': None,
                    'note': 'High Quality Audio (MP3)',
                    'delivery': 'server',
                    'direct_url': None,
                })
            else:
                seen_res = set()
                available_formats = cast(list, info.get('formats') or [])
                available_formats.sort(key=lambda x: x.get('height') or 0, reverse=True)

                def pick_progressive(height: int):
                    candidates = [
                        f for f in available_formats
                        if f.get('height') == height
                        and f.get('vcodec') not in (None, 'none')
                        and f.get('acodec') not in (None, 'none')
                        and 'm3u8' not in str(f.get('protocol') or '')
                        and 'dash' not in str(f.get('protocol') or '')
                    ]
                    if not candidates:
                        return None
                    candidates.sort(
                        key=lambda x: (
                            str(x.get('protocol') or '').startswith('https'),
                            x.get('ext') == 'mp4',
                            x.get('tbr') or 0,
                            x.get('filesize') or 0,
                        ),
                        reverse=True,
                    )
                    best = candidates[0]
                    return {
                        'format_id': best.get('format_id'),
                        'ext': best.get('ext'),
                        'url': best.get('url'),
                        'filesize': best.get('filesize'),
                        'note': best.get('format_note') or best.get('format')
                    }
                
                for f in available_formats:
                    height = f.get('height')
                    if not height: continue
                    if height not in seen_res and height in [2160, 1440, 1080, 720, 480]:
                        progressive = pick_progressive(height) if height <= 720 else None
                        direct_url = progressive.get('url') if progressive else None
                        delivery = 'direct' if direct_url else 'server'
                        formats_list.append({
                            'format_id': progressive.get('format_id') if progressive and progressive.get('format_id') else f['format_id'],
                            'height': height,
                            'ext': progressive.get('ext') if progressive and progressive.get('ext') else 'mp4',
                            'filesize': progressive.get('filesize') if progressive else f.get('filesize'),
                            'note': (progressive.get('note') if progressive else f.get('format_note')),
                            'delivery': delivery,
                            'direct_url': direct_url,
                        })
                        seen_res.add(height)

            return {
                "title": info.get('title'),
                "thumbnail": info.get('thumbnail'),
                "duration": info.get('duration'),
                "formats": formats_list
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=humanize_yt_dlp_error(e))

async def process_download(job_id: str, url: str, height: Optional[int], dl_type: str):
    # REMOVED SEMAPHORE: Ab yahan koi Lock nahi hai.
    try:
        jobs[job_id]['status'] = 'starting'
        jobs[job_id]['progress'] = 0
        
        output_template = os.path.join(TEMP_DIR, f"{job_id}.%(ext)s")
        
        ydl_opts = {
            'outtmpl': output_template,
            'progress_hooks': [lambda d: ydl_progress_hook(d, job_id)],
            'postprocessor_hooks': [lambda d: ydl_postprocessor_hook(d, job_id)],
            **get_ytdlp_common_opts(),
        }

        if dl_type == 'audio':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            format_str = f"bestvideo[height={height}]+bestaudio/best[height={height}]/best"
            ydl_opts.update({
                'format': format_str,
                'merge_output_format': 'mp4',
            })
        
        jobs[job_id]['status'] = 'downloading'

        with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
            info = ydl.extract_info(url, download=True)
            
            filename = None
            if dl_type == 'audio':
                 potential_filename = os.path.join(TEMP_DIR, f"{job_id}.mp3")
                 if os.path.exists(potential_filename):
                     filename = potential_filename
            else:
                 potential_filename = os.path.join(TEMP_DIR, f"{job_id}.mp4")
                 if os.path.exists(potential_filename):
                     filename = potential_filename
            
            if filename and os.path.exists(filename):
                jobs[job_id]['status'] = 'completed'
                jobs[job_id]['progress'] = 100
                for phase in jobs[job_id].get('phases', {}).values():
                    phase['status'] = 'completed'
                    phase['progress'] = 100.0
                update_overall_progress(job_id)
                jobs[job_id]['filename'] = filename
                jobs[job_id]['title'] = info.get('title', 'download')
            else:
                raise Exception("File not found after processing")
                
    except Exception as e:
        print(f"Job {job_id} failed: {e}")
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['error'] = humanize_yt_dlp_error(e)
        for phase in jobs[job_id].get('phases', {}).values():
            if phase['status'] != 'completed':
                phase['status'] = 'failed'

        try:
            for name in os.listdir(TEMP_DIR):
                if name.startswith(job_id + "."):
                    try:
                        os.remove(os.path.join(TEMP_DIR, name))
                    except Exception:
                        pass
        except Exception:
            pass

@app.post("/download")
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    if not re.match(YOUTUBE_REGEX, request.url):
         raise HTTPException(status_code=400, detail="Invalid URL")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'queued',
        'url': request.url,
        'type': request.type,
        'progress': 0,
        'phases': initialize_phases(request.type)
    }
    background_tasks.add_task(process_download, job_id, request.url, request.height, request.type)
    return {"job_id": job_id}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]

@app.get("/file/{job_id}")
async def get_file(job_id: str, background_tasks: BackgroundTasks):
    if job_id not in jobs or jobs[job_id]['status'] != 'completed':
        raise HTTPException(status_code=400, detail="File not ready")
    
    filename = jobs[job_id]['filename']
    title = jobs[job_id]['title']
    ext = "mp3" if jobs[job_id]['type'] == 'audio' else "mp4"
    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
    safe_filename = f"{safe_title}.{ext}"
    
    background_tasks.add_task(delete_file_delayed, filename, delay=600)

    file_size = None
    try:
        file_size = os.path.getsize(filename)
    except Exception:
        file_size = None

    headers = {
        "Content-Disposition": f'attachment; filename="{safe_filename}"',
    }
    if file_size is not None:
        headers["Content-Length"] = str(file_size)

    return StreamingResponse(
        stream_file_and_cleanup(job_id, filename),
        media_type=media_type,
        headers=headers,
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)