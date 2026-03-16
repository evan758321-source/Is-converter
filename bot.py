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

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

DISCORD_TOKEN    = os.environ["DISCORD_TOKEN"]
COOKIES_B64      = os.environ.get("YOUTUBE_COOKIES_B64", "")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

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

def write_cookies_file(tmp_dir):
    if not COOKIES_B64:
        return None
    try:
        raw  = base64.b64decode(COOKIES_B64).decode("utf-8")
        path = os.path.join(tmp_dir, "cookies.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
        return path
    except Exception as e:
        print(f"[warn] Could not write cookies file: {e}")
        return None

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def download_wav(url, out_dir, cookies_path):
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "ffmpeg_location": FFMPEG_PATH,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if cookies_path:
        ydl_opts["cookiefile"] = cookies_path
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info  = ydl.extract_info(url, download=True)
        title = info.get("title", "audio")
        safe  = sanitize_filename(title)
        fpath = os.path.join(out_dir, safe + ".wav")
        if not os.path.isfile(fpath):
            for f in os.listdir(out_dir):
                if f.endswith(".wav"):
                    fpath = os.path.join(out_dir, f)
                    break
    return fpath, title

def generate_go_downloader(url, title):
    safe = sanitize_filename(title).replace('"', '\\"')
    return f'''package main

import (
\t"fmt"
\t"os"
\t"os/exec"
)

func main() {{
\turl := "{url}"
\tout := "{safe}.wav"
\targs := []string{{"--no-playlist", "-x", "--audio-format", "wav", "-o", out, url}}
\tfmt.Println("Downloading:", url)
\tcmd := exec.Command("yt-dlp", args...)
\tcmd.Stdout = os.Stdout
\tcmd.Stderr = os.Stderr
\tif err := cmd.Run(); err != nil {{
\t\tfmt.Fprintln(os.Stderr, "Error:", err)
\t\tos.Exit(1)
\t}}
\tfmt.Println("Saved to:", out)
}}
'''

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = app_commands.CommandTree(client)

@tree.command(name="yt2wav", description="Convert a YouTube video to a WAV file")
@app_commands.describe(url="The YouTube URL to convert")
async def yt2wav(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)
    with tempfile.TemporaryDirectory() as tmp_dir:
        cookies_path = write_cookies_file(tmp_dir)
        try:
            loop = asyncio.get_event_loop()
            fpath, title = await loop.run_in_executor(None, download_wav, url, tmp_dir, cookies_path)
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
                file=discord.File(fpath, filename=sanitize_filename(title) + ".wav")
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
                file=discord.File(go_path, filename=go_name)
            )

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {client.user} — slash commands synced.")

if __name__ == "__main__":
    t = threading.Thread(target=run_keepalive, daemon=True)
    t.start()
    client.run(DISCORD_TOKEN)
