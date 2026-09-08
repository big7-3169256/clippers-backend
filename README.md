# Quiet Riot Club — Clipper Backend

The ffmpeg piece n8n Cloud can't do itself. One endpoint: give it a
video URL + a list of segments, get back cut, captioned, vertical
(9:16) clips.

## Run it locally

Requires Python 3.11+ and `ffmpeg` installed on your machine
(`brew install ffmpeg` on Mac, `apt install ffmpeg` on Linux).

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Test it:

```bash
curl -X POST http://localhost:8000/process-clip \
  -H "Content-Type: application/json" \
  -d '{
        "video_url": "https://example.com/podcast.mp4",
        "segments": [
          {"start": 30, "end": 45, "caption": "Wait, WHAT?"},
          {"start": 120, "end": 150, "caption": "This part is gold"}
        ]
      }'
```

Response gives you back a URL per clip under `/clips/<job_id>/clip_1.mp4`.

## Deploy it (free tier)

This ships with a `Dockerfile` — Render, Railway, and Fly.io can all
build and run it directly from that.

1. Push this folder to a GitHub repo.
2. On Render/Railway/Fly: "New Web Service" → connect the repo →
   it detects the Dockerfile automatically.
3. Set the environment variable `PUBLIC_BASE_URL` to whatever URL
   the platform gives you (e.g. `https://quiet-riot-clipper.onrender.com`)
   — this is what makes the returned clip URLs work.
4. Note: free tiers spin down when idle, so the first request after
   inactivity will be slow (cold start) — expected at this stage.

## Wiring it into n8n

In your n8n Cloud workflow, after the Groq transcription + highlight
steps, add an **HTTP Request** node:

- Method: `POST`
- URL: `https://<your-deployed-url>/process-clip`
- Body (JSON):
  ```json
  {
    "video_url": "{{ $json.video_url }}",
    "segments": {{ $json.segments }}
  }
  ```
  where `segments` is the JSON array your highlight-picking LLM step
  produced: `[{"start": .., "end": .., "caption": ".."}, ...]`

The response's `clips` array gives you the finished, hosted clip
URLs to post wherever you want next.

## Known limitations (fine for MVP, fix later)

- Vertical crop is a center crop — works well for single-speaker
  talking-head footage, less well for footage with important content
  off-center.
- No auth on the endpoint yet — add an API key check before this is
  public-facing with real traffic.
- Clips accumulate on disk — call `DELETE /jobs/{job_id}` to clean up,
  or add a cron job later.
