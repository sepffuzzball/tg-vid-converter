import ipaddress
import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from version import __version__

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tg-vid-converter")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS = [
    cid.strip()
    for cid in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
    if cid.strip()
]
ALLOWED_SUBNET = os.environ.get("ALLOWED_SUBNET", "")
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "ffmpeg")
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot limit
PORT = int(os.environ.get("PORT", 8080))

# Parse allowed subnet once at startup
_allowed_network: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None
if ALLOWED_SUBNET:
    try:
        _allowed_network = ipaddress.ip_network(ALLOWED_SUBNET, strict=False)
        logger.info(f"IP filtering enabled: allowing requests from {_allowed_network}")
    except ValueError as exc:
        logger.error(f"Invalid ALLOWED_SUBNET '{ALLOWED_SUBNET}': {exc}")


# ---------------------------------------------------------------------------
# Lifespan – startup / shutdown hooks
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN is not set – /convert and /convert-url will fail")
    if not TELEGRAM_CHAT_IDS:
        logger.warning("TELEGRAM_CHAT_IDS is not set – /convert and /convert-url will fail")
    logger.info(f"Configured chat IDs: {TELEGRAM_CHAT_IDS or '(none)'}")
    logger.info(f"FFmpeg path: {FFMPEG_PATH}")
    logger.info(f"Max file size: {MAX_FILE_SIZE // (1024 * 1024)} MB")
    if _allowed_network:
        logger.info(f"Subnet filter: {_allowed_network}")
    else:
        logger.info("Subnet filter: disabled (ALLOWED_SUBNET not set)")
    yield
    logger.info("Shutting down…")


app = FastAPI(title="Telegram Video Converter", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware – subnet-based IP filtering
# ---------------------------------------------------------------------------
@app.middleware("http")
async def subnet_filter_middleware(request: Request, call_next):
    if _allowed_network:
        client_host = request.client.host if request.client else "unknown"
        try:
            client_ip = ipaddress.ip_address(client_host)
            if client_ip not in _allowed_network:
                logger.warning(f"Rejected request from {client_host} (outside {_allowed_network})")
                return JSONResponse(status_code=403, content={"detail": "Forbidden"})
        except ValueError:
            logger.warning(f"Could not parse client IP: {client_host}")
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _convert_video(
    input_path: str,
    output_path: str,
    width: Optional[int],
    height: Optional[int],
    crf: int,
    preset: str,
) -> None:
    """Run ffmpeg to convert *input_path* → *output_path*."""
    cmd = [FFMPEG_PATH, "-i", input_path]

    if width or height:
        w = width if width else -1
        h = height if height else -1
        cmd.extend(["-vf", f"scale={w}:{h}"])

    cmd.extend([
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-y",
        output_path,
    ])

    logger.info(f"Running ffmpeg: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out after 300 s")
        raise RuntimeError("ffmpeg conversion timed out")

    if result.returncode != 0:
        logger.error(f"ffmpeg failed: {result.stderr[-500:]}")
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr[-1000:]}")

    logger.info(f"ffmpeg finished – output {os.path.getsize(output_path)} bytes")


async def _send_to_telegram(
    video_path: str,
    caption: Optional[str] = None,
    chat_ids: Optional[list[str]] = None,
) -> list[dict]:
    """Upload *video_path* to every chat in *chat_ids* (defaults to env-configured IDs)."""
    target_ids = chat_ids or TELEGRAM_CHAT_IDS

    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not set.")
    if not target_ids:
        raise HTTPException(status_code=500, detail="No chat IDs configured (set TELEGRAM_CHAT_IDS).")

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    results: list[dict] = []

    async with httpx.AsyncClient(timeout=120) as client:
        for chat_id in target_ids:
            with open(video_path, "rb") as fh:
                files = {"video": (Path(video_path).name, fh)}
                data: dict = {"chat_id": str(chat_id)}
                if caption:
                    data["caption"] = caption

                logger.info(f"Sending video to chat_id={chat_id}")
                resp = await client.post(api_url, files=files, data=data)

            if resp.status_code != 200:
                logger.error(f"Telegram API error for chat_id={chat_id}: {resp.text[:300]}")
                results.append({"chat_id": chat_id, "success": False, "error": resp.text[:500]})
            else:
                logger.info(f"Video sent successfully to chat_id={chat_id}")
                results.append({"chat_id": chat_id, "success": True})

    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": __version__,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS),
    }


@app.post("/convert")
async def convert_and_send(
    file: UploadFile = File(...),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    crf: int = Form(23),
    preset: str = Form("medium"),
    format: str = Form("mp4"),
    send_to_telegram: bool = Form(True),
    caption: Optional[str] = Form(None),
    chat_ids: Optional[str] = Form(None),
):
    """Receive a video file upload, convert with ffmpeg, send to Telegram."""
    logger.info(f"Received file upload: {file.filename} ({file.content_type})")

    tmp_dir = tempfile.mkdtemp()
    try:
        suffix = Path(file.filename).suffix if file.filename else ".mp4"
        input_path = os.path.join(tmp_dir, f"input{suffix}")
        output_ext = f".{format}" if not format.startswith(".") else format
        output_path = os.path.join(tmp_dir, f"output{output_ext}")

        contents = await file.read()
        if len(contents) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024 * 1024)} MB.",
            )

        logger.info(f"Saved input: {len(contents)} bytes")
        with open(input_path, "wb") as fh:
            fh.write(contents)

        _convert_video(input_path, output_path, width, height, crf, preset)

        output_size = os.path.getsize(output_path)
        if output_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"Converted file ({output_size // (1024 * 1024)} MB) exceeds Telegram's 50 MB limit.",
            )

        if not send_to_telegram:
            return JSONResponse(status_code=200, content={
                "message": "Conversion successful (not sent to Telegram).",
                "output_size": output_size,
            })

        target_ids = [c.strip() for c in chat_ids.split(",") if c.strip()] if chat_ids else None
        results = await _send_to_telegram(output_path, caption, target_ids)

        return JSONResponse(status_code=200, content={
            "message": "Video converted and sent to Telegram.",
            "output_size": output_size,
            "results": results,
        })
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/convert-url")
async def convert_url_and_send(
    url: str = Form(...),
    width: Optional[int] = Form(None),
    height: Optional[int] = Form(None),
    crf: int = Form(23),
    preset: str = Form("medium"),
    format: str = Form("mp4"),
    send_to_telegram: bool = Form(True),
    caption: Optional[str] = Form(None),
    chat_ids: Optional[str] = Form(None),
    auth_token: Optional[str] = Form(None),
):
    """Download a video from a URL, convert with ffmpeg, send to Telegram.

    Useful when Home Assistant / Unifi Protect can provide a direct video URL
    instead of uploading the file.
    """
    logger.info(f"Downloading video from URL: {url}")

    tmp_dir = tempfile.mkdtemp()
    try:
        # Build headers for Unifi Protect / protected URLs
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        async with httpx.AsyncClient(timeout=120, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to download video: HTTP {resp.status_code}",
                )
            contents = resp.content
            content_type = resp.headers.get("content-type", "")

        if len(contents) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"Downloaded file too large ({len(contents) // (1024 * 1024)} MB).",
            )

        logger.info(f"Downloaded {len(contents)} bytes (content-type: {content_type})")

        # Guess suffix from content-type
        if "webm" in content_type:
            suffix = ".webm"
        elif "quicktime" in content_type:
            suffix = ".mov"
        elif "x-matroska" in content_type:
            suffix = ".mkv"
        else:
            suffix = ".mp4"

        input_path = os.path.join(tmp_dir, f"input{suffix}")
        output_ext = f".{format}" if not format.startswith(".") else format
        output_path = os.path.join(tmp_dir, f"output{output_ext}")

        with open(input_path, "wb") as fh:
            fh.write(contents)

        _convert_video(input_path, output_path, width, height, crf, preset)

        output_size = os.path.getsize(output_path)
        if output_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"Converted file ({output_size // (1024 * 1024)} MB) exceeds Telegram's 50 MB limit.",
            )

        if not send_to_telegram:
            return JSONResponse(status_code=200, content={
                "message": "Conversion successful (not sent to Telegram).",
                "output_size": output_size,
            })

        target_ids = [c.strip() for c in chat_ids.split(",") if c.strip()] if chat_ids else None
        results = await _send_to_telegram(output_path, caption, target_ids)

        return JSONResponse(status_code=200, content={
            "message": "Video converted and sent to Telegram.",
            "output_size": output_size,
            "results": results,
        })
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting server on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
