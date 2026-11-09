# Video Clone AI — GitHub + Render

This app accepts a user-uploaded video (up to 30 minutes; upload cap defaults to 500 MB), transcribes its speech, writes a fresh original narration, generates AI voice, and renders a 1920x1080 MP4 using the uploaded video's visuals with its original audio replaced.

## Important use and deployment limits

- Upload only videos you own or have permission to adapt. This app does not copy another creator's voice or generate a byte-for-byte duplicate.
- "1080p" is the output canvas size. Lower-resolution source video is upscaled and does not gain real detail. Vertical/square sources are padded to 16:9.
- Render web services have finite CPU, RAM, request-size, timeout, and disk limits. A 30-minute 1080p job can be resource-intensive. The default upload cap is 500 MB; increase only if your Render plan and proxy limits support it.
- This MVP keeps job status in process memory and temporary files under `/tmp`. A service restart loses active jobs and output files. For production, use a background worker plus object storage (S3/R2) and a persistent queue.
- OpenAI API usage (transcription, text generation, and speech) is billed separately from ChatGPT subscriptions.
- Never commit your real API key to GitHub. Add it in Render Environment.

## Render setup

Create a Render Web Service from this GitHub repository.

- Runtime: Python
- Build command: `apt-get update && apt-get install -y ffmpeg && pip install -r requirements.txt`
- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`

Environment variables:
- `PYTHON_VERSION` = `3.11.11`
- `OPENAI_API_KEY` = your secret API key from the OpenAI API platform
- `OPENAI_MODEL` = `gpt-4.1-mini`
- `OPENAI_TTS_MODEL` = `gpt-4o-mini-tts`
- `OPENAI_TTS_VOICE` = `alloy`
- `MAX_UPLOAD_MB` = `500`

## Local run

Install FFmpeg, then:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Put your API key in .env; load it into your shell before running.
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

## Workflow

1. Upload MP4/MOV/M4V/WebM.
2. Choose output language and narration style.
3. Click **Clone Video** once.
4. Keep the page open while progress is polled.
5. Download the rendered MP4 when complete.

The generated script is original narration based on the source transcript. The rendered video reuses the uploaded visuals and replaces the original audio with generated narration.
