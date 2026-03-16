import discord
from discord import app_commands
import yt_dlp
import os
import asyncio
import tempfile
import re
import base64
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import imageio_ffmpeg

# Point yt-dlp at the bundled ffmpeg binary (no system ffmpeg needed)
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# ─── Config ───────────────────────────────────────────────────────────────────
DISCORD_TOKEN   = os.environ["DISCORD_TOKEN"]
COOKIES_B64     = os.environ.get("YOUTUBE_COOKIES_B64", "")   # base64-encoded cookies.txt
MAX_UPLOAD_MB   = 25          # Discord free-tier limit (MB)
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# ─── Keep-alive HTTP server (so Render "Web Service" stays awake) ──────────────
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, *_):   # silence the default access log
        pass

def run_keepalive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()

# ─── Helpers ──────────────────────────────────────────────────────────────────
def write_cookies_file(tmp_dir: str) -> str | None:
    """Decode the base64 env-var cookie blob and write it to a temp file."""
    if not COOKIES_B64:
        return None
    try:
        raw = base64.b64decode(COOKIES_B64).decode("utf-8")
        path = os.path.join(tmp_dir, "cookies.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        return path
    except Exception as e:
        print(f"[warn] Could not write cookies file: {e}")
        return None


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def download_wav(url: str, out_dir: str, cookies_path: str | None) -> tuple[str, str]:
    """
    Download the audio from *url* as a WAV file into *out_dir*.
    Returns (filepath, video_title).
    """
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "wav",
        }],
        "ffmpeg_location": FFMPEG_PATH,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info   = ydl.extract_info(url, download=True)
        title  = info.get("title", "audio")
        # yt-dlp always writes the final file as .wav after post-processing
        safe   = sanitize_filename(title)
        fpath  = os.path.join(out_dir, safe + ".wav")
        if not os.path.isfile(fpath):
            # fallback: find any .wav in the directory
            for f in os.listdir(out_dir):
                if f.endswith(".wav"):
                    fpath = os.path.join(out_dir, f)
                    break
    return fpath, title


# ─── Bot ──────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)


@tree.command(name="yt2wav", description="Convert a YouTube video to a WAV file")
@app_commands.describe(url="The YouTube URL to convert")
async def yt2wav(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)   # may take a while

    with tempfile.TemporaryDirectory() as tmp_dir:
        cookies_path = write_cookies_file(tmp_dir)

        # ── Download ──────────────────────────────────────────────────────────
        try:
            loop     = asyncio.get_event_loop()
            fpath, title = await loop.run_in_executor(
                None, download_wav, url, tmp_dir, cookies_path
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ **Download failed.**\n```{e}```\n"
                "Make sure the URL is a valid, public YouTube link.\n"
                "If you keep seeing bot-detection errors, refresh your `cookies.txt`."
            )
            return

        if not os.path.isfile(fpath):
            await interaction.followup.send("❌ Conversion finished but the WAV file was not found. Please try again.")
            return

        size = os.path.getsize(fpath)

        # ── Under Discord limit → send directly ───────────────────────────────
        if size <= MAX_UPLOAD_BYTES:
            await interaction.followup.send(
                f"✅ **{title}** — here's your WAV file!",
                file=discord.File(fpath, filename=sanitize_filename(title) + ".wav")
            )

        # ── Over limit → send the raw file as a Go source attachment ──────────
        else:
            size_mb = size / (1024 * 1024)
            go_code = generate_go_downloader(url, title, cookies_path)
            go_filename = sanitize_filename(title) + "_downloader.go"
            go_path = os.path.join(tmp_dir, go_filename)
            with open(go_path, "w", encoding="utf-8") as f:
                f.write(go_code)

            await interaction.followup.send(
                f"⚠️ **{title}**\n"
                f"The WAV is **{size_mb:.1f} MB** — too large for Discord's {MAX_UPLOAD_MB} MB limit.\n\n"
                f"Here's a **Go file** you can run locally to download the WAV directly to your machine:\n"
                f"```\ngo run {go_filename}\n```",
                file=discord.File(go_path, filename=go_filename)
            )


def generate_go_downloader(url: str, title: str, cookies_path: str | None) -> str:
    """Return Go source code that downloads the YouTube audio as WAV locally."""
    safe_title   = sanitize_filename(title).replace('"', '\\"')
    cookies_note = "// No cookies — add --cookies cookies.txt if you get bot-detection errors" \
                   if not cookies_path else \
                   "// Place your cookies.txt next to this file, then add: \"--cookies\", \"cookies.txt\","

    return f'''// Auto-generated by yt2wav Discord bot
// Run with:  go run {safe_title}_downloader.go
//
// Requirements:
//   • Go  1.21+        https://go.dev/dl/
//   • yt-dlp           https://github.com/yt-dlp/yt-dlp/releases
//   • ffmpeg           https://ffmpeg.org/download.html
//
// Make sure yt-dlp and ffmpeg are in your PATH (or same directory).

package main

import (
\t"fmt"
\t"os"
\t"os/exec"
)

func main() {{
\turl := "{url}"
\tout := "{safe_title}.wav"

\t{cookies_note}
\targs := []string{{
\t\t"--no-playlist",
\t\t"-x",
\t\t"--audio-format", "wav",
\t\t"-o", out,
\t\turl,
\t}}

\tfmt.Println("⬇  Downloading:", url)
\tcmd := exec.Command("yt-dlp", args...)
\tcmd.Stdout = os.Stdout
\tcmd.Stderr = os.Stderr
\tif err := cmd.Run(); err != nil {{
\t\tfmt.Fprintln(os.Stderr, "Error:", err)
\t\tos.Exit(1)
\t}}
\tfmt.Println("✅ Saved to:", out)
}}
'''


@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user} — slash commands synced.")


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start keep-alive HTTP server in a background thread
    t = threading.Thread(target=run_keepalive, daemon=True)
    t.start()
    print(f"🌐 Keep-alive server started on port {os.environ.get('PORT', 8080)}")

    client.run(DISCORD_TOKEN)
