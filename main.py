"""
Quiet Riot Club - Clipper Backend
----------------------------------
Single-endpoint FastAPI service that takes a video URL + a list of
clip segments (start/end/caption) and returns cut, captioned,
vertically-reframed clips using ffmpeg.

This is the piece n8n Cloud can't do itself (no shell access there),
so n8n calls this service over HTTP to do the actual video work.

Run locally:
    uvicorn main:app --host 0.0.0.0 --port 8000

Requires ffmpeg to be installed on the host / in the container.
"""

import os
import uuid
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(title="Quiet Riot Club Clipper Backend")

# Where finished clips get written. Served statically so n8n / a
# frontend can fetch them by URL after processing.
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/clips", StaticFiles(directory=str(OUTPUT_DIR)), name="clips")

# Public base URL of this service, used to build the URLs returned
# to the caller (n8n). Set this to your real Render/Railway/Fly URL
# once deployed, e.g. https://your-app.onrender.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class Segment(BaseModel):
    start: float = Field(..., description="Clip start time in seconds")
    end: float = Field(..., description="Clip end time in seconds")
    caption: Optional[str] = Field(
        None, description="Short hook text to burn onto the clip (optional)"
    )


class ProcessRequest(BaseModel):
    video_url: str = Field(..., description="Direct URL to the source video file")
    segments: List[Segment] = Field(..., description="Clip segments to extract")
    vertical: bool = Field(
        True, description="If true, crop/scale output to 1080x1920 (9:16)"
    )


class ClipResult(BaseModel):
    caption: Optional[str]
    start: float
    end: float
    url: str


class ProcessResponse(BaseModel):
    clips: List[ClipResult]


class ExtractAudioRequest(BaseModel):
    video_url: str = Field(..., description="Video URL (direct file or YouTube link)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _download_direct_video(url: str, dest: Path) -> None:
    """Stream-download a plain video file URL to a local temp file."""
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
    except requests.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Failed to download video: {e}")


def _download_youtube_video(url: str, dest: Path) -> None:
    """Download a YouTube video via yt-dlp, merging separate video/audio
    streams (the norm for most videos now) into a single mp4 with ffmpeg."""
    ydl_opts = {
        # Best video + best audio, merged into mp4. Falls back to a
        # single combined stream if that's all that's available.
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(dest.with_suffix("")),  # yt-dlp appends the extension itself
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=f"Failed to download YouTube video: {e}")

    # yt-dlp writes to <outtmpl>.mp4 after merging; normalize to dest.
    produced = dest.with_suffix(".mp4")
    if produced.exists() and produced != dest:
        produced.rename(dest)

    if not dest.exists():
        raise HTTPException(
            status_code=500,
            detail="YouTube download reported success but no file was produced",
        )


def _download_video(url: str, dest: Path) -> None:
    """Download the source video, routing to yt-dlp for YouTube links."""
    if _is_youtube_url(url):
        _download_youtube_video(url, dest)
    else:
        _download_direct_video(url, dest)


def _escape_drawtext(text: str) -> str:
    """Escape text so it's safe to embed in an ffmpeg drawtext filter."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\u2019")  # swap straight quotes to avoid breaking the filter
        .replace("%", "\\%")
    )


def _build_filtergraph(vertical: bool, caption: Optional[str]) -> str:
    """
    Build the ffmpeg -filter_complex graph for one clip.

    For vertical output, instead of cropping (which slices off faces on
    wide/multi-person footage), we shrink the whole frame to fit the
    1080-wide canvas and fill the empty top/bottom with a blurred, zoomed
    copy of the same footage as a background. Nothing gets cut off.
    """
    caption_filter = ""
    if caption:
        safe = _escape_drawtext(caption)
        caption_filter = (
            ",drawtext="
            f"text='{safe}':"
            "fontcolor=white:fontsize=64:"
            "box=1:boxcolor=black@0.6:boxborderw=20:"
            "x=(w-text_w)/2:y=120:"
            "line_spacing=8"
        )

    if vertical:
        return (
            "[0:v]split=2[base][fg];"
            "[base]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=25[bg];"
            "[fg]scale=1080:-2[fgscaled];"
            "[bg][fgscaled]overlay=(W-w)/2:(H-h)/2"
            f"{caption_filter}[outv]"
        )

    # Non-vertical: just apply the caption directly, if any.
    return f"[0:v]null{caption_filter}[outv]"



def _cut_clip(
    source_path: Path,
    out_path: Path,
    start: float,
    end: float,
    vertical: bool,
    caption: Optional[str],
) -> None:
    duration = max(end - start, 0.1)
    filter_complex = _build_filtergraph(vertical, caption)

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", str(source_path),
        "-t", str(duration),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        str(out_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"ffmpeg failed for segment {start}-{end}: {result.stderr[-2000:]}",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/extract-audio")
def extract_audio(req: ExtractAudioRequest):
    """
    Downloads the source video (direct URL or YouTube) and returns just
    the audio track as an mp3 file. This is the step n8n calls before
    sending audio to a transcription API (e.g. Groq Whisper), since
    those APIs need an audio file, not a video URL.
    """
    job_id = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    audio_path = job_dir / "audio.mp3"

    with tempfile.TemporaryDirectory() as tmp:
        source_path = Path(tmp) / "source.mp4"
        _download_video(req.video_url, source_path)

        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(source_path),
            "-vn",  # no video
            "-acodec", "libmp3lame",
            "-q:a", "4",  # reasonable quality/size tradeoff for speech
            str(audio_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"ffmpeg failed extracting audio: {result.stderr[-2000:]}",
            )

    return FileResponse(
        path=str(audio_path),
        media_type="audio/mpeg",
        filename="audio.mp3",
    )


@app.post("/process-clip", response_model=ProcessResponse)
def process_clip(req: ProcessRequest):
    if not req.segments:
        raise HTTPException(status_code=400, detail="No segments provided")

    job_id = uuid.uuid4().hex[:10]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        source_path = Path(tmp) / "source.mp4"
        _download_video(req.video_url, source_path)

        results: List[ClipResult] = []
        for i, seg in enumerate(req.segments):
            out_name = f"clip_{i+1}.mp4"
            out_path = job_dir / out_name

            _cut_clip(
                source_path=source_path,
                out_path=out_path,
                start=seg.start,
                end=seg.end,
                vertical=req.vertical,
                caption=seg.caption,
            )

            results.append(
                ClipResult(
                    caption=seg.caption,
                    start=seg.start,
                    end=seg.end,
                    url=f"{PUBLIC_BASE_URL}/clips/{job_id}/{out_name}",
                )
            )

    return ProcessResponse(clips=results)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """Optional cleanup endpoint so old clips don't pile up on disk."""
    job_dir = OUTPUT_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
        return {"deleted": job_id}
    raise HTTPException(status_code=404, detail="Job not found")
