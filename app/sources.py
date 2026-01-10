import os
from typing import Dict, List, Optional, Tuple

import spotipy
import yt_dlp
from spotipy.oauth2 import SpotifyClientCredentials

from config import DOWNLOAD_DIR, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
from utils import make_track_filename, sanitize_filename

YDL_OPTS_DOWNLOAD = {
    "format": "bestaudio/best",
    "quiet": True,
    "noprogress": True,
    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).150B.%(ext)s"),
    "postprocessors": [],
}

YDL_OPTS_YT_INFO = {
    "quiet": True,
    "noprogress": True,
    "skip_download": True,
}

YDL_OPTS_DOWNLOAD_SINGLE = {
    **YDL_OPTS_DOWNLOAD,
    "noplaylist": True,
}

YDL_OPTS_YT_INFO_SINGLE = {
    **YDL_OPTS_YT_INFO,
    "noplaylist": True,
}


def make_spotify_client() -> Optional[spotipy.Spotify]:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    auth = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
    )
    return spotipy.Spotify(auth_manager=auth)


def is_spotify_playlist(url: str) -> bool:
    return "open.spotify.com/playlist" in url


def is_youtube_url(url: str) -> bool:
    return ("youtube.com" in url or "youtu.be" in url)


def get_spotify_playlist_info(playlist_url: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Return playlist name and list of (artist, title) from a Spotify playlist URL."""
    sp = make_spotify_client()
    if sp is None:
        raise RuntimeError("Spotify credentials missing; cannot process Spotify playlists.")
    
    # Extract playlist ID from URL
    playlist_id = playlist_url.split('/')[-1].split('?')[0]
    
    # Get playlist info
    playlist_info = sp.playlist(playlist_id, fields="name")
    playlist_name = playlist_info.get("name", "Unknown Playlist")
    
    # Get tracks
    results = sp.playlist_items(playlist_url, additional_types=["track"], fields="items(track(name,artists(name))),next")
    tracks: List[Tuple[str, str]] = []

    def extract_items(items):
        for item in items:
            t = item.get("track") or {}
            name = t.get("name") or ""
            artists = t.get("artists") or []
            artist_names = ", ".join(a.get("name", "") for a in artists if a)
            if name and artist_names:
                tracks.append((artist_names, name))

    extract_items(results.get("items", []))
    while results.get("next"):
        results = sp.next(results)
        extract_items(results.get("items", []))
    
    return playlist_name, tracks


def yt_search_best(artist: str, title: str) -> Optional[dict]:
    """Use yt-dlp to find the most relevant video for 'artist - title'."""
    query = f"ytsearch1:{artist} - {title}"
    with yt_dlp.YoutubeDL({**YDL_OPTS_YT_INFO, "default_search": "auto"}) as ydl:
        info = ydl.extract_info(query, download=False)
        if not info:
            return None
        if "entries" in info and info["entries"]:
            return info["entries"][0]
        return info


def yt_resolve_playlist_or_video(url: str) -> Tuple[str, List[dict]]:
    """Return playlist name and list of entries (video dicts) for a YouTube URL."""
    with yt_dlp.YoutubeDL(YDL_OPTS_YT_INFO) as ydl:
        info = ydl.extract_info(url, download=False)
        
        # Get playlist name if it's a playlist
        playlist_name = "YouTube Playlist"
        if "title" in info:
            playlist_name = info["title"]
        elif "entries" in info and len(info.get("entries", [])) > 1:
            playlist_name = f"YouTube Playlist ({len(info['entries'])} videos)"
        elif "entries" in info and info["entries"]:
            playlist_name = info["entries"][0].get("title", "YouTube Video")
        
        if "entries" in info:
            return playlist_name, [e for e in info["entries"] if e]
        return playlist_name, [info]


def yt_get_single_entry(url: str) -> dict:
    """Return a single video entry for a YouTube URL."""
    with yt_dlp.YoutubeDL(YDL_OPTS_YT_INFO_SINGLE) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("Could not extract video info.")
        if "entries" in info and info["entries"]:
            return info["entries"][0]
        return info


def download_if_needed_for_entry(entry: dict, download_opts: Optional[Dict] = None) -> str:
    """Download entry if not present. Return local filepath."""
    title = entry.get("title") or "audio"
    expected_prefix = sanitize_filename(title)
    
    # Check if file already exists
    for ext in (".webm", ".m4a", ".mp3", ".opus"):
        candidate = os.path.join(DOWNLOAD_DIR, expected_prefix + ext)
        if os.path.exists(candidate):
            return candidate

    # Download if not found
    opts = download_opts or YDL_OPTS_DOWNLOAD
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([entry.get("webpage_url") or entry.get("url")])

    # Find the downloaded file
    for ext in (".webm", ".m4a", ".mp3", ".opus"):
        candidate = os.path.join(DOWNLOAD_DIR, expected_prefix + ext)
        if os.path.exists(candidate):
            return candidate

    # Fallback: find newest file
    files = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR)]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise RuntimeError("Download appears to have failed.")
    newest = max(files, key=os.path.getmtime)
    return newest


def ensure_download_for_spotify_track(artist: str, title: str) -> str:
    """Download a Spotify track from YouTube if not already present."""
    expected_file = make_track_filename(DOWNLOAD_DIR, artist, title)
    if os.path.exists(expected_file):
        return expected_file

    entry = yt_search_best(artist, title)
    if not entry:
        raise RuntimeError(f"Could not find YouTube result for: {artist} - {title}")
    return download_if_needed_for_entry(entry)
