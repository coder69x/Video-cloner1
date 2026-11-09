import os
import uuid
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from openai import OpenAI

APP_DIR = Path(__file__).parent
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/video-clone-jobs"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "500")) * 1024 * 1024
MAX_VIDEO_SECONDS = 30 * 60
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}

app = FastAPI(title="Video Clone AI")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

# MVP job store: use one Render instance while jobs are running.
jobs = {}
jobs_lock = threading.Lock()


def run_cmd(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Command failed")[-5000:])
    return result.stdout.strip()


def probe_video(path: Path):
    raw = run_cmd([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of", "json", str(path)
    ])
    data = json.loads(raw)
    duration = float(data.get("format", {}).get("duration", 0))
    if duration <= 0:
        raise RuntimeError("Video duration could not be detected.")
    if duration > MAX_VIDEO_SECONDS:
        raise RuntimeError("Video 30 minutes se lambi hai. Maximum 30 minutes allowed.")
    video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video_stream:
        raise RuntimeError("Uploaded file mein video stream nahi mili.")
    return duration


def update_job(job_id, **kwargs):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(kwargs)


def split_text(text, limit=3500):
    # Keep chunks under the speech endpoint's input limit, preferring sentence boundaries.
    chunks, current = [], ""
    for sentence in text.replace("\n", " ").split(". "):
        sentence = sentence.strip()
        if not sentence:
            continue
        piece = sentence if sentence.endswith((".", "!", "?")) else sentence + "."
        if len(current) + len(piece) + 1 > limit and current:
            chunks.append(current.strip())
            current = ""
        current += piece + " "
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text[:limit]]


def create_script(client, transcript, language, style, duration_seconds):
    minutes = max(1, round(duration_seconds / 60))
    prompt = f"""
You are a professional video scriptwriter. Create a fresh, original narration script in {language}.
The source transcript is only for understanding the topic and key facts. Do not copy sentences,
distinctive phrasing, jokes, or long passages. Do not claim to reproduce the original creator.
Style: {style}. Target duration: approximately {minutes} minutes.
Write natural spoken narration with a strong opening, clear sections, smooth transitions and a
memorable ending. Avoid headings that would be spoken aloud, stage directions, citations, and
markdown. Do not add unsupported facts. If the transcript is incomplete, stay within what it supports.
Aim for about {minutes * 125} spoken words.
SOURCE TRANSCRIPT:
{transcript[:100000]}
"""
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=min(12000, max(1500, minutes * 190))
    )
    return response.choices[0].message.content.strip()


def transcribe_video_audio(client, video_path: Path, job_dir: Path):
    audio_path = job_dir / "source_audio.mp3"
    run_cmd([
        "ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1",
        "-ar", "16000", "-b:a", "48k", str(audio_path)
    ])
    # Split into 8-minute chunks to keep each upload comfortably below common API limits.
    chunk_pattern = job_dir / "audio_%03d.mp3"
    run_cmd([
        "ffmpeg", "-y", "-i", str(audio_path), "-f", "segment",
        "-segment_time", "480", "-c", "copy", str(chunk_pattern)
    ])
    chunks = sorted(job_dir.glob("audio_*.mp3"))
    transcript_parts = []
    for index, chunk in enumerate(chunks, start=1):
        with chunk.open("rb") as audio_file:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )
        transcript_parts.append(str(result))
        update_job(job_dir.name, progress=20 + int(index / max(1, len(chunks)) * 25),
                   message=f"Audio transcript ho raha hai ({index}/{len(chunks)})...")
    if not transcript_parts or not "".join(transcript_parts).strip():
        raise RuntimeError("Video se speech transcript nahi mil saka. Video mein clear speech honi chahiye.")
    return "\n".join(transcript_parts)


def generate_voice(client, script, job_dir: Path):
    pieces = split_text(script)
    audio_files = []
    for index, piece in enumerate(pieces, start=1):
        response = client.audio.speech.create(
            model=os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=os.getenv("OPENAI_TTS_VOICE", "alloy"),
            input=piece,
            response_format="mp3"
        )
        out = job_dir / f"voice_{index:03d}.mp3"
        response.stream_to_file(str(out))
        audio_files.append(out)
        update_job(job_dir.name, progress=50 + int(index / max(1, len(pieces)) * 25),
                   message=f"AI voice ban rahi hai ({index}/{len(pieces)})...")
    concat_file = job_dir / "voice_concat.txt"
    concat_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in audio_files), encoding="utf-8")
    final_audio = job_dir / "narration.mp3"
    run_cmd(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(final_audio)])
    return final_audio


def process_job(job_id, input_path, language, style):
    job_dir = WORK_DIR / job_id
    try:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Render Environment mein OPENAI_API_KEY set nahi hai.")
        client = OpenAI(api_key=api_key)
        update_job(job_id, status="processing", progress=5, message="Video check ho raha hai...")
        duration = probe_video(input_path)
        update_job(job_id, progress=10, message="Video ka audio extract ho raha hai...")
        transcript = transcribe_video_audio(client, input_path, job_dir)
        update_job(job_id, progress=47, message="Nayi original script likhi ja rahi hai...")
        script = create_script(client, transcript, language, style, duration)
        (job_dir / "script.txt").write_text(script, encoding="utf-8")
        update_job(job_id, progress=50, message="AI voice generate ho rahi hai...")
        voice_path = generate_voice(client, script, job_dir)
        update_job(job_id, progress=78, message="1080p video render ho raha hai...")
        output_path = job_dir / "cloned_video_1080p.mp4"
        # Reuse the uploaded visuals (only if you own/have permission); replace original audio with new AI narration.
        run_cmd([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(input_path), "-i", str(voice_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
            str(output_path)
        ])
        update_job(job_id, status="completed", progress=100, message="Video ready hai!", output=f"/api/jobs/{job_id}/download")
    except Exception as exc:
        update_job(job_id, status="failed", message=str(exc), error=str(exc))
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/", response_class=HTMLResponse)
def home():
    return (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/clone")
async def start_clone(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    language: str = Form("Hindi"),
    style: str = Form("engaging explainer")
):
    ext = Path(video.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "MP4, MOV, M4V ya WebM video upload karo.")
    job_id = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"source{ext}"
    total = 0
    try:
        with input_path.open("wb") as dest:
            while True:
                chunk = await video.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File bahut badi hai. Maximum {MAX_UPLOAD_BYTES // (1024*1024)} MB allowed.")
                dest.write(chunk)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        await video.close()

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued", "progress": 0, "message": "Job queue mein hai.",
            "output": None, "error": None
        }
    background_tasks.add_task(process_job, job_id, input_path, language, style)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nahi mila. Service restart hui ho sakti hai; dobara upload karein.")
    return job


@app.get("/api/jobs/{job_id}/download")
def download_result(job_id: str):
    path = WORK_DIR / job_id / "cloned_video_1080p.mp4"
    if not path.exists():
        raise HTTPException(404, "Video abhi ready nahi hai.")
    return FileResponse(path, media_type="video/mp4", filename="cloned_video_1080p.mp4")


@app.get("/api/jobs/{job_id}/script")
def download_script(job_id: str):
    path = WORK_DIR / job_id / "script.txt"
    if not path.exists():
        raise HTTPException(404, "Script abhi ready nahi hai.")
    return FileResponse(path, media_type="text/plain", filename="original_script.txt")
