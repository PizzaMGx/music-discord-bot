import os

TOKEN = os.environ.get("DISCORD_TOKEN", "")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
PLAYLISTS_DIR = os.environ.get("PLAYLISTS_DIR", "/playlists")
SONGS_FILE = os.path.join(PLAYLISTS_DIR, "songs.json")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID")


def ensure_dirs() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(PLAYLISTS_DIR, exist_ok=True)
