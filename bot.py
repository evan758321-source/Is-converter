import discord
from discord import app_commands
import os
import asyncio
import tempfile
import re
import base64
import threading
import subprocess
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import imageio_ffmpeg

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

DISCORD_TOKEN    = os.environ["DISCORD_TOKEN"]
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

COBALT_API = os.environ.get("COBALT_API", "https://cobalt-production-cf4e.up.railway.app")

# ---------------------------------------------------------------------------
# Keep-alive HTTP server (for Render / uptime pingers)
# ---------------------------------------------------------------------------

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def log_message(self, *_):
        pass


def run_keepalive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    print(f"Keep-alive HTTP server listening on port {port}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def download_wav(url, out_dir):
    """
    Downloads audio via Cobalt v10 API, saves as MP3, converts to WAV.
    Returns (wav_path, title).
    """

    # --- Step 1: Ask Cobalt for a download link ---
    try:
        resp = requests.post(
            COBALT_API,
            json={
                "url": url,
                "downloadMode": "audio",
                "audioFormat": "mp3",
                "audioBitrate": "256",
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise RuntimeError(f"Cobalt API request failed: {e}")

    status = data.get("status")

    if status == "error":
        raise RuntimeError(f"Cobalt error: {data.get('error', {}).get('code', 'unknown')} — {data}")

    if status not in ("tunnel", "redirect"):
        raise RuntimeError(f"Unexpected Cobalt response: {data}")

    audio_url = data.get("url")
    if not audio_url:
        raise RuntimeError(f"Cobalt returned no URL: {data}")

    # Try to pull a filename/title from Cobalt's response
    filename_hint = data.get("filename", "audio")
    title = re.sub(r'\.[^.]+$', '', filename_hint)  # strip extension

    # --- Step 2: Download the audio stream ---
    try:
        audio_resp = requests.get(audio_url, timeout=120, stream=True)
        audio_resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to download audio from Cobalt tunnel: {e}")

    mp3_path = os.path.join(out_dir, "audio.mp3")
    with open(mp3_path, "wb") as f:
        for chunk in audio_resp.iter_content(chunk_size=8192):
            f.write(chunk)

    if not os.path.isfile(mp3_path) or os.path.getsize(mp3_path) == 0:
        raise RuntimeError("Downloaded MP3 is empty or missing.")

    # --- Step 3: Convert MP3 → WAV ---
    wav_path = os.path.join(out_dir, "audio.wav")
    result = subprocess.run(
        [FFMPEG_PATH, "-y", "-i", mp3_path, "-ar", "44100", wav_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg conversion failed:\n{result.stderr.decode()}")

    return wav_path, title


def generate_go_downloader(url, title):
    safe = sanitize_filename(title).replace('"', '\\"')
    return f'''package main

import (
\t"fmt"
\t"io"
\t"net/http"
\t"os"
)

func main() {{
\turl := "{url}"
\tout := "{safe}.wav"

\t// Download via Cobalt v10
\tclient := &http.Client{{}}
\treq, _ := http.NewRequest("POST", "https://api.cobalt.tools/", nil)
\treq.Header.Set("Content-Type", "application/json")
\treq.Header.Set("Accept", "application/json")
\tbody := `{{"url":"` + url + `","downloadMode":"audio","audioFormat":"mp3"}}`
\treq.Body = io.NopCloser(strings.NewReader(body))

\tresp, err := client.Do(req)
\tif err != nil {{
\t\tfmt.Fprintln(os.Stderr, "Error:", err)
\t\tos.Exit(1)
\t}}
\tdefer resp.Body.Close()
\tfmt.Println("Saved to:", out)
}}
'''


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


@tree.command(name="yt2wav", description="Convert a YouTube video to a WAV file")
@app_commands.describe(url="The YouTube URL to convert")
async def yt2wav(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            loop = asyncio.get_event_loop()
            fpath, title = await loop.run_in_executor(
                None, download_wav, url, tmp_dir
            )
        except Exception as e:
            await interaction.followup.send(f"❌ **Download failed.**\n```{e}```")
            return

        if not os.path.isfile(fpath):
            await interaction.followup.send("❌ WAV file not found after conversion.")
            return

        size = os.path.getsize(fpath)

        if size <= MAX_UPLOAD_BYTES:
            await interaction.followup.send(
                f"✅ **{title}**",
                file=discord.File(fpath, filename=sanitize_filename(title) + ".wav"),
            )
        else:
            size_mb = size / (1024 * 1024)
            go_code = generate_go_downloader(url, title)
            go_name = sanitize_filename(title) + "_downloader.go"
            go_path = os.path.join(tmp_dir, go_name)
            with open(go_path, "w", encoding="utf-8") as f:
                f.write(go_code)
            await interaction.followup.send(
                f"⚠️ **{title}** is {size_mb:.1f} MB — too large for Discord.\n"
                f"Run this Go file locally:\n```go run {go_name}```",
                file=discord.File(go_path, filename=go_name),
            )


# ---------------------------------------------------------------------------
# on_ready
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s).")
    except discord.HTTPException as e:
        print(f"[warn] Command sync failed: {e}")

    print(f"✅ Logged in as {client.user}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    t = threading.Thread(target=run_keepalive, daemon=True)
    t.start()
    client.run(DISCORD_TOKEN)
