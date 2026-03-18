import discord
from discord import app_commands
import os
import asyncio
import tempfile
import re
import threading
import subprocess
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import imageio_ffmpeg

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
RAPIDAPI_KEY  = os.environ["RAPIDAPI_KEY"]
MAX_UPLOAD_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Keep-alive HTTP server
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
# Shared helpers
# ---------------------------------------------------------------------------

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def convert_to_wav(input_path, out_dir):
    """Convert any audio/video file to WAV using ffmpeg."""
    wav_path = os.path.join(out_dir, "audio.wav")
    result = subprocess.run(
        [FFMPEG_PATH, "-y", "-i", input_path, "-ar", "44100", "-ac", "2", wav_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg conversion failed:\n{result.stderr.decode()}")
    return wav_path


def upload_to_gofile(fpath, title):
    """Upload a file to GoFile and return the download page URL."""
    server_resp = requests.get("https://api.gofile.io/servers", timeout=10)
    server_resp.raise_for_status()
    server = server_resp.json()["data"]["servers"][0]["name"]
    with open(fpath, "rb") as f:
        upload_resp = requests.post(
            f"https://{server}.gofile.io/contents/uploadfile",
            files={"file": (sanitize_filename(title) + ".wav", f, "audio/wav")},
            timeout=300,
        )
    upload_resp.raise_for_status()
    return upload_resp.json()["data"]["downloadPage"]


async def send_wav_result(interaction, fpath, title):
    """Send the WAV to Discord, falling back to GoFile if too large."""
    size = os.path.getsize(fpath)

    if size <= MAX_UPLOAD_BYTES:
        await interaction.followup.send(
            f"✅ **{title}**",
            file=discord.File(fpath, filename=sanitize_filename(title) + ".wav"),
        )
    else:
        size_mb = size / (1024 * 1024)
        await interaction.followup.send(
            f"⚠️ **{title}** is {size_mb:.1f} MB — too large for Discord. Uploading to GoFile..."
        )
        try:
            link = upload_to_gofile(fpath, title)
            await interaction.followup.send(f"✅ **{title}**\n📎 Download here:\n{link}")
        except Exception as e:
            await interaction.followup.send(f"❌ GoFile upload failed.\n```{e}```")


def download_with_ytdlp(url, out_dir, filename_prefix):
    """
    Generic yt-dlp downloader used by TikTok, SoundCloud, Instagram, and X.
    Returns (wav_path, title).
    """
    import yt_dlp

    raw_template = os.path.join(out_dir, filename_prefix)

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": raw_template + ".%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_PATH,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    title = info.get("title") or info.get("uploader") or filename_prefix
    ext   = info.get("ext", "mp4")
    downloaded_path = f"{raw_template}.{ext}"

    if not os.path.isfile(downloaded_path) or os.path.getsize(downloaded_path) == 0:
        raise RuntimeError("yt-dlp downloaded an empty or missing file.")

    wav_path = convert_to_wav(downloaded_path, out_dir)
    return wav_path, title


# ---------------------------------------------------------------------------
# YouTube helpers
# ---------------------------------------------------------------------------

def extract_video_id(url):
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"(?:youtu\.be\/)([0-9A-Za-z_-]{11})",
        r"(?:embed\/)([0-9A-Za-z_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract video ID from URL: {url}")


def download_wav(url, out_dir):
    video_id = extract_video_id(url)

    # --- Step 1: Request MP3 conversion from RapidAPI ---
    response = requests.get(
        "https://youtube-mp36.p.rapidapi.com/dl",
        headers={
            "X-RapidAPI-Key": RAPIDAPI_KEY,
            "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com",
        },
        params={"id": video_id},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("status") != "ok":
        import time
        for _ in range(5):
            time.sleep(3)
            response = requests.get(
                "https://youtube-mp36.p.rapidapi.com/dl",
                headers={
                    "X-RapidAPI-Key": RAPIDAPI_KEY,
                    "X-RapidAPI-Host": "youtube-mp36.p.rapidapi.com",
                },
                params={"id": video_id},
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "ok":
                break
        else:
            raise RuntimeError(f"RapidAPI conversion failed: {data}")

    mp3_url = data.get("link")
    title   = data.get("title", "audio")

    if not mp3_url:
        raise RuntimeError(f"No download link returned: {data}")

    # --- Step 2: Download the MP3 ---
    audio_resp = requests.get(mp3_url, timeout=120, stream=True)
    audio_resp.raise_for_status()

    mp3_path = os.path.join(out_dir, "audio.mp3")
    with open(mp3_path, "wb") as f:
        for chunk in audio_resp.iter_content(chunk_size=8192):
            f.write(chunk)

    if not os.path.isfile(mp3_path) or os.path.getsize(mp3_path) == 0:
        raise RuntimeError("Downloaded MP3 is empty or missing.")

    # --- Step 3: Convert MP3 -> WAV ---
    wav_path = convert_to_wav(mp3_path, out_dir)
    return wav_path, title


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
            fpath, title = await loop.run_in_executor(None, download_wav, url, tmp_dir)
        except Exception as e:
            await interaction.followup.send(f"❌ **Download failed.**\n```{e}```")
            return

        if not os.path.isfile(fpath):
            await interaction.followup.send("❌ WAV file not found after conversion.")
            return

        await send_wav_result(interaction, fpath, title)


@tree.command(name="tt2wav", description="Convert a TikTok video to a WAV file")
@app_commands.describe(url="The TikTok URL to convert")
async def tt2wav(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            loop = asyncio.get_event_loop()
            fpath, title = await loop.run_in_executor(
                None, download_with_ytdlp, url, tmp_dir, "tiktok_audio"
            )
        except Exception as e:
            await interaction.followup.send(f"❌ **Download failed.**\n```{e}```")
            return

        if not os.path.isfile(fpath):
            await interaction.followup.send("❌ WAV file not found after conversion.")
            return

        await send_wav_result(interaction, fpath, title)


@tree.command(name="sc2wav", description="Convert a SoundCloud track to a WAV file")
@app_commands.describe(url="The SoundCloud URL to convert")
async def sc2wav(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            loop = asyncio.get_event_loop()
            fpath, title = await loop.run_in_executor(
                None, download_with_ytdlp, url, tmp_dir, "sc_audio"
            )
        except Exception as e:
            await interaction.followup.send(f"❌ **Download failed.**\n```{e}```")
            return

        if not os.path.isfile(fpath):
            await interaction.followup.send("❌ WAV file not found after conversion.")
            return

        await send_wav_result(interaction, fpath, title)


@tree.command(name="ig2wav", description="Convert an Instagram reel/video to a WAV file")
@app_commands.describe(url="The Instagram URL to convert")
async def ig2wav(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            loop = asyncio.get_event_loop()
            fpath, title = await loop.run_in_executor(
                None, download_with_ytdlp, url, tmp_dir, "ig_audio"
            )
        except Exception as e:
            await interaction.followup.send(f"❌ **Download failed.**\n```{e}```")
            return

        if not os.path.isfile(fpath):
            await interaction.followup.send("❌ WAV file not found after conversion.")
            return

        await send_wav_result(interaction, fpath, title)


@tree.command(name="x2wav", description="Convert an X (Twitter) video to a WAV file")
@app_commands.describe(url="The X/Twitter URL to convert")
async def x2wav(interaction: discord.Interaction, url: str):
    await interaction.response.defer(thinking=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            loop = asyncio.get_event_loop()
            fpath, title = await loop.run_in_executor(
                None, download_with_ytdlp, url, tmp_dir, "x_audio"
            )
        except Exception as e:
            await interaction.followup.send(f"❌ **Download failed.**\n```{e}```")
            return

        if not os.path.isfile(fpath):
            await interaction.followup.send("❌ WAV file not found after conversion.")
            return

        await send_wav_result(interaction, fpath, title)


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
