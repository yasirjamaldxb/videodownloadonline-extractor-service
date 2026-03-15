from __future__ import annotations

import json
import os
import subprocess
from typing import Any
from urllib.parse import urlparse
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

SUPPORTED_HOST_SUFFIXES = (
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "x.com",
    "twitter.com",
    "streamain.com",
    "dailymotion.com",
    "dai.ly",
    "vimeo.com",
    "bilibili.tv",
)
SAFE_EXTENSIONS = {"mp4", "webm", "m4a", "mp3"}
DEFAULT_TIMEOUT_SECONDS = 45

app = FastAPI(title="video-extractor-service", version="1.1.0")


class ExtractRequest(BaseModel):
    url: str
    rightsConfirmed: bool = False


class HealthResponse(BaseModel):
    status: str
    extractorMode: str
    ytDlpVersion: str | None = None


@app.get("/")
def root() -> dict[str, str]:
    return {"status": "ok"}


@app.head("/")
def root_head() -> None:
    return None


def normalize_host(hostname: str) -> str:
    host = hostname.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_supported_social_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = normalize_host(parsed.hostname or "")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in SUPPORTED_HOST_SUFFIXES)


def bytes_to_label(size: int | float | None) -> str:
    if not size:
        return "Size unknown"

    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1

    if value >= 100 or idx == 0:
        return f"{value:.0f} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def extractor_failure_status(detail: str) -> int:
    text = detail.lower()
    if "failed to establish a new connection" in text or "actively refused it" in text:
        return 422
    if "login required" in text or "cookies-from-browser" in text or "empty media response" in text:
        return 422
    if "requested content is not available" in text or "not available" in text:
        return 422
    if "video #1 is unavailable" in text or "no video could be found" in text:
        return 422
    if "rate-limit" in text or "too many requests" in text:
        return 429
    if "timed out" in text:
        return 504
    return 502


def resolve_yt_dlp_command() -> str:
    env_path = (os.getenv("YTDLP_PATH") or "").strip()
    if env_path:
        return env_path

    local_binary = Path.cwd() / "bin" / "yt-dlp"
    if local_binary.exists():
        return str(local_binary)

    return "yt-dlp"


def run_yt_dlp_process(url: str, use_impersonate: bool) -> dict[str, Any]:
    args = [
        resolve_yt_dlp_command(),
        "--dump-single-json",
        "--no-playlist",
        "--no-warnings",
        "--skip-download",
        "--geo-bypass",
        "--extractor-retries",
        "2",
        "--fragment-retries",
        "2",
        "--socket-timeout",
        "15",
        url,
    ]

    if use_impersonate:
        args[6:6] = ["--impersonate", "chrome"]

    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
            text=True,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Timed out while extracting media.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="yt-dlp not available on extractor host.") from exc

    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip() or f"yt-dlp failed with exit code {result.returncode}"
        raise HTTPException(status_code=extractor_failure_status(detail), detail=detail)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Invalid JSON from yt-dlp.") from exc


def run_yt_dlp(url: str) -> dict[str, Any]:
    try:
        return run_yt_dlp_process(url, use_impersonate=True)
    except HTTPException as exc:
        text = str(exc.detail).lower()
        if "impersonate" in text and ("unsupported" in text or "not available" in text):
            return run_yt_dlp_process(url, use_impersonate=False)
        raise


def resolve_yt_dlp_version() -> str | None:
    try:
        result = subprocess.run(
            [resolve_yt_dlp_command(), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            text=True,
            encoding="utf-8",
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    return result.stdout.strip() or None


def map_formats(payload: dict[str, Any]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []

    for item in payload.get("formats", []) or []:
        ext = item.get("ext")
        url = item.get("url")
        if not ext or not url or ext not in SAFE_EXTENSIONS:
            continue

        size = item.get("filesize") or item.get("filesize_approx")
        quality = f"{item.get('height')}p" if item.get("height") else item.get("format_note") or item.get("format") or "Standard"

        mapped.append(
            {
                "id": item.get("format_id") or f"{ext}-{item.get('height') or 'na'}-{item.get('fps') or 'na'}",
                "ext": ext,
                "qualityLabel": quality,
                "sizeLabel": bytes_to_label(size),
                "fps": item.get("fps"),
                "hasVideo": bool(item.get("vcodec") and item.get("vcodec") != "none"),
                "hasAudio": bool(item.get("acodec") and item.get("acodec") != "none"),
                "url": url,
            }
        )

    mapped.sort(
        key=lambda x: (
            0 if x["hasVideo"] else 1,
            -(int(str(x["qualityLabel"]).replace("p", "")) if str(x["qualityLabel"]).replace("p", "").isdigit() else 0),
            0 if x["hasAudio"] else 1,
        )
    )

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in mapped:
        key = f"{row['ext']}-{row['qualityLabel']}-{row['hasVideo']}-{row['hasAudio']}-{row['url']}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    if deduped:
        return deduped[:20]

    fallback_url = payload.get("url")
    fallback_ext = payload.get("ext")
    if fallback_url and fallback_ext:
        return [
            {
                "id": "default",
                "ext": fallback_ext,
                "qualityLabel": "Default",
                "sizeLabel": "Size unknown",
                "fps": None,
                "hasAudio": True,
                "url": fallback_url,
            }
        ]

    return []


def verify_api_key(x_extractor_key: str | None) -> None:
    configured = (os.getenv("EXTRACTOR_API_KEY") or "").strip()
    if not configured:
        return
    if x_extractor_key != configured:
        raise HTTPException(status_code=401, detail="Invalid extractor API key.")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        extractorMode="external-service",
        ytDlpVersion=resolve_yt_dlp_version(),
    )


@app.head("/health")
def health_head() -> None:
    return None


@app.post("/extract")
def extract(payload: ExtractRequest, x_extractor_key: str | None = Header(default=None)) -> dict[str, Any]:
    verify_api_key(x_extractor_key)

    source_url = payload.url.strip()
    if not is_supported_social_url(source_url):
        raise HTTPException(
            status_code=400,
            detail="Only TikTok, Instagram, Facebook, X, Streamain, Dailymotion, Vimeo, and Bilibili links are accepted.",
        )

    raw = run_yt_dlp(source_url)
    formats = map_formats(raw)
    if not formats:
        raise HTTPException(status_code=404, detail="No downloadable formats found.")

    return {
        "sourceUrl": source_url,
        "title": (raw.get("title") or "Untitled video")[:120],
        "thumbnail": raw.get("thumbnail"),
        "duration": raw.get("duration"),
        "uploader": raw.get("uploader"),
        "webpageUrl": raw.get("webpage_url") or source_url,
        "formats": formats,
    }
