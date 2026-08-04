"""
AI Lab (Student Edition) — Backend
Free, open pipeline: prompt -> script -> scene images -> voiceover -> subtitles -> final MP4

Free services used (no paid keys required):
  - Groq API        : script/scene generation (free tier, get key at console.groq.com)
  - Pollinations.ai : text-to-image, no key needed
  - edge-tts         : text-to-speech, no key needed (uses Microsoft Edge voices)
  - FFmpeg           : video assembly (Ken Burns effect, subtitles, concat, audio mix)
"""

import os
import json
import uuid
import shutil
import asyncio
import subprocess
import urllib.parse
from pathlib import Path
from typing import Optional

import httpx
import edge_tts
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
EDGE_VOICE = os.getenv("EDGE_VOICE", "en-US-AndrewNeural")

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="AI Lab (Student Edition)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job status store (fine for a single small free-tier instance)
JOBS: dict[str, dict] = {}


class CreateJobRequest(BaseModel):
    topic: str
    num_scenes: int = 5
    voice: Optional[str] = None
    aspect: str = "9:16"  # "9:16" (shorts/reels) or "16:9" (landscape)


# ---------------------------------------------------------------------------
# Step 1: Script + scene generation via Groq (free LLM API)
# ---------------------------------------------------------------------------
async def generate_scenes(topic: str, num_scenes: int) -> list[dict]:
    if not GROQ_API_KEY:
        return [
            {
                "narration": f"{topic} — part {i+1}. This is placeholder narration; "
                              f"add a free Groq API key to generate real scripts.",
                "image_prompt": f"{topic}, educational illustration, scene {i+1}, clean flat style"
            }
            for i in range(num_scenes)
        ]

    system_prompt = (
        "You are a video script writer for short educational videos aimed at students. "
        f"Given a topic, write exactly {num_scenes} short scenes. "
        "Return ONLY valid JSON: a list of objects, each with 'narration' "
        "(1-2 spoken sentences, simple clear language) and 'image_prompt' "
        "(a vivid visual description for an AI image generator, no text/words in the image). "
        "No markdown, no commentary, JSON only."
    )

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Topic: {topic}"},
                ],
                "temperature": 0.7,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        content = content.strip().strip("`")
        if content.startswith("json"):
            content = content[4:]
        scenes = json.loads(content)
        return scenes[:num_scenes]


# ---------------------------------------------------------------------------
# Step 2: Scene images via Pollinations.ai (free, no key) — WITH RETRIES
# ---------------------------------------------------------------------------
async def generate_image(image_prompt: str, out_path: Path, width: int, height: int):
    encoded = urllib.parse.quote(image_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&nologo=true"
    last_error = None
    async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
        for attempt in range(4):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                out_path.write_bytes(resp.content)
                return
            except Exception as e:
                last_error = e
                await asyncio.sleep(4)
    raise last_error


# ---------------------------------------------------------------------------
# Step 3: Voiceover via edge-tts (free, no key)
# ---------------------------------------------------------------------------
async def generate_voice(text: str, out_path: Path, voice: str) -> float:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)],
        capture_output=True, text=True
    )
    return float(result.stdout.strip() or 2.0)


# ---------------------------------------------------------------------------
# Step 4: Assemble each scene as a Ken-Burns clip with burned-in subtitle
# ---------------------------------------------------------------------------
def build_scene_clip(image_path: Path, audio_path: Path, subtitle_text: str,
                      duration: float, out_path: Path, width: int, height: int):
    safe_text = subtitle_text.replace("'", "\u2019").replace(":", "\\:").replace(",", "\\,")
    zoom_frames = max(int(duration * 25), 25)

    vf = (
        f"scale={width*2}:{height*2},"
        f"zoompan=z='min(zoom+0.0012,1.2)':d={zoom_frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps=25,"
        f"drawtext=text='{safe_text}':fontcolor=white:fontsize=42:"
        f"box=1:boxcolor=black@0.55:boxborderw=18:"
        f"x=(w-text_w)/2:y=h-th-90:line_spacing=8:"
        f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-vf", vf,
        "-t", str(duration),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        str(out_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def concat_clips(clip_paths: list[Path], out_path: Path):
    list_file = out_path.parent / "concat_list.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clip_paths))
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(out_path)
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def run_pipeline(job_id: str, req: CreateJobRequest):
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    JOBS[job_id] = {"status": "generating_script", "progress": 5}

    width, height = (720, 1280) if req.aspect == "9:16" else (1280, 720)
    voice = req.voice or EDGE_VOICE

    try:
        scenes = await generate_scenes(req.topic, req.num_scenes)
        JOBS[job_id] = {"status": "generating_assets", "progress": 15}

        clip_paths = []
        for i, scene in enumerate(scenes):
            img_path = job_dir / f"scene_{i}.jpg"
            audio_path = job_dir / f"scene_{i}.mp3"
            clip_path = job_dir / f"clip_{i}.mp4"

            await generate_image(scene["image_prompt"], img_path, width, height)
            duration = await generate_voice(scene["narration"], audio_path, voice)
            build_scene_clip(img_path, audio_path, scene["narration"],
                              duration, clip_path, width, height)
            clip_paths.append(clip_path)

            JOBS[job_id] = {
                "status": "generating_assets",
                "progress": 15 + int(70 * (i + 1) / len(scenes)),
            }

        JOBS[job_id] = {"status": "assembling_video", "progress": 90}
        final_path = job_dir / "final.mp4"
        concat_clips(clip_paths, final_path)

        JOBS[job_id] = {"status": "done", "progress": 100, "file": str(final_path)}

    except Exception as e:
        JOBS[job_id] = {"status": "error", "progress": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.post("/api/jobs")
async def create_job(req: CreateJobRequest, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {"status": "queued", "progress": 0}
    bg.add_task(run_pipeline, job_id, req)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "job not found")
    return JOBS[job_id]


@app.get("/api/jobs/{job_id}/video")
async def job_video(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "video not ready")
    return FileResponse(job["file"], media_type="video/mp4", filename=f"{job_id}.mp4")


@app.get("/api/health")
async def health():
    return {"ok": True, "groq_configured": bool(GROQ_API_KEY)}


# Serve the frontend
static_dir = BASE_DIR.parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
