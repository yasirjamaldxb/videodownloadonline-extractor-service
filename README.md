# VideoDownloadOnline Extractor Service

Dedicated extractor service for platforms that are unreliable in the Vercel runtime, including Bilibili.

## Runtime
- FastAPI
- Docker
- yt-dlp nightly
- ffmpeg

## Local run
```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

## Health
- `/health`

## Env vars
- `EXTRACTOR_API_KEY` (optional but recommended)
- `YTDLP_CHANNEL=nightly`
