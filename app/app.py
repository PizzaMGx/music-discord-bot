import os
import re
import json
import asyncio
import functools
from typing import List, Optional, Tuple, Dict

import discord
from discord.ext import commands

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

# ========= Configuration =========
TOKEN = os.environ.get("DISCORD_TOKEN", "")
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
PLAYLISTS_DIR = os.environ.get("PLAYLISTS_DIR", "/playlists")

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN env var.")
if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
    print("[warn] Spotify credentials not set. Spotify links will not work.")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(PLAYLISTS_DIR, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True  # Ensure voice state intent is enabled

# Configure bot with better connection settings
bot = commands.Bot(
    command_prefix="/", 
    intents=intents, 
    heartbeat_timeout=60.0,
    # Add connection pool settings
    connector=None,  # Will use default with better settings
)

# ========= Helpers =========

def sanitize_filename(name: str) -> str:
    # keep it short & filesystem-safe
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150]

def make_track_filename(artist: str, title: str) -> str:
    safe = sanitize_filename(f"{artist} - {title}")
    return os.path.join(DOWNLOAD_DIR, f"{safe}.webm")

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

FFMPEG_OPTIONS = {
    "options": "-vn -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
}

# ========= Playlist Storage =========
class PlaylistManager:
    def __init__(self, playlists_dir: str):
        self.playlists_dir = playlists_dir
    
    def save_playlist(self, name: str, tracks: List[Dict], guild_id: int, playlist_url: str = None, source_type: str = "unknown"):
        """Save a playlist with list of track dictionaries."""
        playlist_data = {
            "name": name,
            "tracks": tracks,
            "guild_id": guild_id,
            "track_count": len(tracks),
            "playlist_url": playlist_url,
            "source_type": source_type,  # "spotify", "youtube", "manual"
            "shuffle": False
        }
        playlist_file = os.path.join(self.playlists_dir, f"{sanitize_filename(name)}.json")
        with open(playlist_file, 'w', encoding='utf-8') as f:
            json.dump(playlist_data, f, indent=2, ensure_ascii=False)
    
    def load_playlist(self, name: str) -> Optional[Dict]:
        """Load a playlist by name."""
        playlist_file = os.path.join(self.playlists_dir, f"{sanitize_filename(name)}.json")
        if not os.path.exists(playlist_file):
            return None
        try:
            with open(playlist_file, 'r', encoding='utf-8') as f:
                playlist_data = json.load(f)
                
            # Migrate old format to new format if needed
            if playlist_data.get("tracks") and isinstance(playlist_data["tracks"][0], str):
                playlist_data = self._migrate_old_format(playlist_data)
                # Save the migrated format
                with open(playlist_file, 'w', encoding='utf-8') as f:
                    json.dump(playlist_data, f, indent=2, ensure_ascii=False)
                    
            return playlist_data
        except (json.JSONDecodeError, IOError):
            return None
    
    def _migrate_old_format(self, old_data: Dict) -> Dict:
        """Migrate old playlist format to new format."""
        new_tracks = []
        for track_path in old_data.get("tracks", []):
            # Extract name from file path
            filename = os.path.basename(track_path)
            name = os.path.splitext(filename)[0]  # Remove extension
            
            new_tracks.append({
                "name": name,
                "path": track_path,
                "artist": None,
                "title": None
            })
        
        return {
            "name": old_data.get("name", "Unknown"),
            "tracks": new_tracks,
            "guild_id": old_data.get("guild_id"),
            "track_count": len(new_tracks),
            "playlist_url": None,
            "source_type": "migrated",
            "shuffle": old_data.get("shuffle", False)
        }
    
    def list_playlists(self) -> List[Dict]:
        """List all available playlists with basic info."""
        playlists = []
        for filename in os.listdir(self.playlists_dir):
            if filename.endswith('.json'):
                playlist_data = self.load_playlist(filename[:-5])  # Remove .json
                if playlist_data:
                    playlists.append({
                        "name": playlist_data.get("name", "Unknown"),
                        "track_count": playlist_data.get("track_count", 0),
                        "source_type": playlist_data.get("source_type", "unknown"),
                        "has_url": bool(playlist_data.get("playlist_url"))
                    })
        return sorted(playlists, key=lambda x: x["name"])
    
    def delete_playlist(self, name: str) -> bool:
        """Delete a playlist."""
        playlist_file = os.path.join(self.playlists_dir, f"{sanitize_filename(name)}.json")
        if os.path.exists(playlist_file):
            os.remove(playlist_file)
            return True
        return False

playlist_manager = PlaylistManager(PLAYLISTS_DIR)

# ========= Spotify =========
def make_spotify_client() -> Optional[spotipy.Spotify]:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    auth = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
    )
    return spotipy.Spotify(auth_manager=auth)

SP = make_spotify_client()

def is_spotify_playlist(url: str) -> bool:
    return "open.spotify.com/playlist" in url

def is_youtube_url(url: str) -> bool:
    return ("youtube.com" in url or "youtu.be" in url)

def get_spotify_playlist_info(playlist_url: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Return playlist name and list of (artist, title) from a Spotify playlist URL."""
    if SP is None:
        raise RuntimeError("Spotify credentials missing; cannot process Spotify playlists.")
    
    # Extract playlist ID from URL
    playlist_id = playlist_url.split('/')[-1].split('?')[0]
    
    # Get playlist info
    playlist_info = SP.playlist(playlist_id, fields="name")
    playlist_name = playlist_info.get("name", "Unknown Playlist")
    
    # Get tracks
    results = SP.playlist_items(playlist_url, additional_types=["track"], fields="items(track(name,artists(name))),next")
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
        results = SP.next(results)
        extract_items(results.get("items", []))
    
    return playlist_name, tracks

# ========= YouTube (search & download) =========
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

def download_if_needed_for_entry(entry: dict) -> str:
    """Download entry if not present. Return local filepath."""
    title = entry.get("title") or "audio"
    expected_prefix = sanitize_filename(title)
    
    # Check if file already exists
    for ext in (".webm", ".m4a", ".mp3", ".opus"):
        candidate = os.path.join(DOWNLOAD_DIR, expected_prefix + ext)
        if os.path.exists(candidate):
            return candidate

    # Download if not found
    with yt_dlp.YoutubeDL(YDL_OPTS_DOWNLOAD) as ydl:
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
    expected_file = make_track_filename(artist, title)
    if os.path.exists(expected_file):
        return expected_file

    entry = yt_search_best(artist, title)
    if not entry:
        raise RuntimeError(f"Could not find YouTube result for: {artist} - {title}")
    return download_if_needed_for_entry(entry)

# ========= Per-guild Music State =========
class GuildMusic:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.now_playing: Optional[str] = None
        self.player_task: Optional[asyncio.Task] = None
        self.stop_event = asyncio.Event()

    async def start(self, voice_client: discord.VoiceClient):
        if self.player_task and not self.player_task.done():
            return
        self.stop_event.clear()
        self.player_task = asyncio.create_task(self._player_loop(voice_client))

    async def _player_loop(self, vc: discord.VoiceClient):
        while not self.stop_event.is_set():
            try:
                path = await asyncio.wait_for(self.queue.get(), timeout=1800)  # 30 min idle timeout
            except asyncio.TimeoutError:
                if vc and vc.is_connected():
                    await vc.disconnect(force=True)
                break

            # Check if voice client is still connected
            if not vc or not vc.is_connected():
                print("[player] Voice client disconnected, stopping player")
                break

            self.now_playing = os.path.basename(path)
            
            # Check if file exists
            if not os.path.exists(path):
                print(f"[player] File not found: {path}")
                self.now_playing = None
                continue

            try:
                source = discord.FFmpegPCMAudio(path, **FFMPEG_OPTIONS)
                
                # Use a more reliable way to wait for playback to finish
                play_finished = asyncio.Event()
                
                def after_play(err):
                    if err:
                        print(f"[ffmpeg after] error: {err}")
                    bot.loop.call_soon_threadsafe(play_finished.set)

                vc.play(source, after=after_play)
                
                # Wait for playback to complete or voice client to disconnect
                while vc.is_playing() and vc.is_connected() and not self.stop_event.is_set():
                    try:
                        await asyncio.wait_for(play_finished.wait(), timeout=1.0)
                        break
                    except asyncio.TimeoutError:
                        continue
                        
            except Exception as e:
                print(f"[player] Error playing {path}: {e}")
            
            self.now_playing = None
            
            # Small delay between tracks
            await asyncio.sleep(0.5)

    def clear(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break
        self.now_playing = None

    def stop_player(self):
        self.stop_event.set()

MUSIC: dict[int, GuildMusic] = {}

def get_music_state(guild: discord.Guild) -> GuildMusic:
    st = MUSIC.get(guild.id)
    if not st:
        st = GuildMusic(guild)
        MUSIC[guild.id] = st
    return st

async def ensure_voice(ctx) -> discord.VoiceClient:
    if not ctx.author.voice or not ctx.author.voice.channel:
        raise commands.CommandError("You must be **in a voice channel**.")
    
    channel = ctx.author.voice.channel
    vc: discord.VoiceClient = ctx.voice_client
    
    # If already connected to the same channel, return existing connection
    if vc and vc.channel.id == channel.id and vc.is_connected():
        return vc
    
    # Disconnect from different channel if needed
    if vc and vc.is_connected():
        await vc.disconnect(force=False)
        await asyncio.sleep(1)
    
    # Attempt connection with retries
    for attempt in range(3):
        try:
            print(f"[voice] Connection attempt {attempt + 1}/3 to {channel.name}")
            vc = await channel.connect(
                timeout=20.0, 
                reconnect=True, 
                self_deaf=True
            )
            
            # Wait for connection to stabilize
            await asyncio.sleep(2)
            
            if vc.is_connected():
                print(f"[voice] Successfully connected to {channel.name}")
                return vc
            else:
                print(f"[voice] Connection failed (not connected)")
                if vc:
                    await vc.disconnect(force=True)
                    
        except asyncio.TimeoutError:
            print(f"[voice] Timeout on attempt {attempt + 1}")
            if attempt < 2:
                await asyncio.sleep(2)
                continue
        except discord.ClientException as e:
            print(f"[voice] Client exception on attempt {attempt + 1}: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
                continue
        except Exception as e:
            print(f"[voice] Unexpected error on attempt {attempt + 1}: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
                continue
    
    raise commands.CommandError("Failed to connect to voice channel after 3 attempts. This might be a network issue.")

# ========= Commands =========
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}.")

@bot.command(help="Join your current voice channel.")
async def join(ctx):
    vc = await ensure_voice(ctx)
    await ctx.reply(f"Joined **{vc.channel.name}**.")

@bot.command(help="Leave the voice channel and clear queue.")
async def leave(ctx):
    if ctx.voice_client:
        st = get_music_state(ctx.guild)
        st.clear()
        st.stop_player()
        await ctx.voice_client.disconnect(force=True)
        await ctx.reply("Disconnected and cleared the queue.")
    else:
        await ctx.reply("Not connected.")

@bot.command(usage="/download <playlist_url> [playlist_name]", help="Download a Spotify playlist or YouTube link.")
async def download(ctx, url: str, *, playlist_name: str = None):
    """Download and save a playlist for later playback."""
    await ctx.reply("⏳ Starting download...")
    
    downloaded_tracks: List[Dict] = []
    actual_playlist_name: str = playlist_name or "Unknown Playlist"
    source_type: str = "unknown"
    
    try:
        if is_spotify_playlist(url):
            if SP is None:
                raise RuntimeError("Spotify credentials missing; cannot process Spotify playlists.")
            
            source_type = "spotify"
            # Get playlist info and tracks
            actual_playlist_name, tracks = await asyncio.to_thread(get_spotify_playlist_info, url)
            if playlist_name:  # Override with user-provided name
                actual_playlist_name = playlist_name
            
            if not tracks:
                raise RuntimeError("Playlist has no tracks or could not be read.")
            
            # Send progress update
            await ctx.send(f"📥 Downloading **{len(tracks)}** tracks from **{actual_playlist_name}**...")
            
            # Download each track
            failed_downloads = 0
            for i, (artist, title) in enumerate(tracks, 1):
                try:
                    path = await asyncio.to_thread(ensure_download_for_spotify_track, artist, title)
                    downloaded_tracks.append({
                        "name": f"{artist} - {title}",
                        "path": path,
                        "artist": artist,
                        "title": title
                    })
                    
                    # Progress update every 5 songs
                    if i % 5 == 0:
                        await ctx.send(f"⏳ Downloaded {i}/{len(tracks)} tracks...")
                        
                except Exception as e:
                    failed_downloads += 1
                    print(f"Failed to download {artist} - {title}: {e}")
                    continue
            
            if failed_downloads > 0:
                await ctx.send(f"⚠️ Failed to download {failed_downloads} tracks.")

        elif is_youtube_url(url):
            source_type = "youtube"
            actual_playlist_name, entries = await asyncio.to_thread(yt_resolve_playlist_or_video, url)
            if playlist_name:  # Override with user-provided name
                actual_playlist_name = playlist_name
                
            if not entries:
                raise RuntimeError("Could not extract any entries from the YouTube URL.")
            
            await ctx.send(f"📥 Downloading **{len(entries)}** videos from **{actual_playlist_name}**...")
            
            failed_downloads = 0
            for i, entry in enumerate(entries, 1):
                try:
                    path = await asyncio.to_thread(download_if_needed_for_entry, entry)
                    downloaded_tracks.append({
                        "name": entry.get("title", "Unknown"),
                        "path": path,
                        "artist": entry.get("uploader"),
                        "title": entry.get("title")
                    })
                    
                    if i % 5 == 0:
                        await ctx.send(f"⏳ Downloaded {i}/{len(entries)} videos...")
                        
                except Exception as e:
                    failed_downloads += 1
                    print(f"Failed to download {entry.get('title', 'Unknown')}: {e}")
                    continue
            
            if failed_downloads > 0:
                await ctx.send(f"⚠️ Failed to download {failed_downloads} videos.")
        else:
            raise RuntimeError("Provide a **Spotify playlist** or **YouTube** link.")

    except Exception as e:
        await ctx.reply(f"❌ Error: {e}")
        return

    if not downloaded_tracks:
        await ctx.reply("❌ No tracks were successfully downloaded.")
        return

    # Save the playlist with new format
    playlist_manager.save_playlist(actual_playlist_name, downloaded_tracks, ctx.guild.id, url, source_type)
    
    await ctx.reply(f"✅ Downloaded and saved **{len(downloaded_tracks)}** tracks as playlist **{actual_playlist_name}**!")

@bot.command(usage="/update <playlist_name>", help="Update a playlist from its original URL.")
async def update(ctx, *, playlist_name: str):
    """Update an existing playlist from its original URL."""
    # Load existing playlist
    playlist_data = playlist_manager.load_playlist(playlist_name)
    if not playlist_data:
        await ctx.reply(f"❌ Playlist **{playlist_name}** not found.")
        return

    playlist_url = playlist_data.get("playlist_url")
    if not playlist_url:
        await ctx.reply(f"❌ Playlist **{playlist_name}** doesn't have an original URL to update from.")
        return

    await ctx.reply(f"🔄 Updating playlist **{playlist_name}** from original source...")

    try:
        # Get current tracks for comparison
        old_tracks = {track.get("name", ""): track for track in playlist_data.get("tracks", [])}
        
        # Re-download the playlist
        downloaded_tracks: List[Dict] = []
        source_type = playlist_data.get("source_type", "unknown")
        
        if is_spotify_playlist(playlist_url):
            if SP is None:
                raise RuntimeError("Spotify credentials missing; cannot update Spotify playlists.")
            
            # Get updated playlist info
            _, tracks = await asyncio.to_thread(get_spotify_playlist_info, playlist_url)
            
            if not tracks:
                raise RuntimeError("Updated playlist has no tracks or could not be read.")
            
            await ctx.send(f"📥 Checking **{len(tracks)}** tracks for updates...")
            
            new_downloads = 0
            failed_downloads = 0
            
            for i, (artist, title) in enumerate(tracks, 1):
                track_name = f"{artist} - {title}"
                
                try:
                    # Check if track already exists
                    if track_name in old_tracks and os.path.exists(old_tracks[track_name].get("path", "")):
                        # Reuse existing file
                        downloaded_tracks.append(old_tracks[track_name])
                    else:
                        # Download new track
                        path = await asyncio.to_thread(ensure_download_for_spotify_track, artist, title)
                        downloaded_tracks.append({
                            "name": track_name,
                            "path": path,
                            "artist": artist,
                            "title": title
                        })
                        new_downloads += 1
                    
                    # Progress update every 10 songs
                    if i % 10 == 0:
                        await ctx.send(f"⏳ Processed {i}/{len(tracks)} tracks...")
                        
                except Exception as e:
                    failed_downloads += 1
                    print(f"Failed to update {artist} - {title}: {e}")
                    continue
            
            # Report results
            removed_count = len(old_tracks) - (len(downloaded_tracks) - new_downloads)
            result_msg = f"✅ Updated **{playlist_name}**!\n"
            result_msg += f"📊 **{new_downloads}** new tracks downloaded\n"
            if removed_count > 0:
                result_msg += f"🗑️ **{removed_count}** tracks removed from playlist\n"
            if failed_downloads > 0:
                result_msg += f"⚠️ **{failed_downloads}** tracks failed to update"

        elif is_youtube_url(playlist_url):
            # Similar logic for YouTube playlists
            _, entries = await asyncio.to_thread(yt_resolve_playlist_or_video, playlist_url)
            
            if not entries:
                raise RuntimeError("Updated playlist has no videos or could not be read.")
            
            await ctx.send(f"📥 Checking **{len(entries)}** videos for updates...")
            
            new_downloads = 0
            failed_downloads = 0
            
            for i, entry in enumerate(entries, 1):
                track_name = entry.get("title", "Unknown")
                
                try:
                    # Check if track already exists
                    if track_name in old_tracks and os.path.exists(old_tracks[track_name].get("path", "")):
                        downloaded_tracks.append(old_tracks[track_name])
                    else:
                        # Download new track
                        path = await asyncio.to_thread(download_if_needed_for_entry, entry)
                        downloaded_tracks.append({
                            "name": track_name,
                            "path": path,
                            "artist": entry.get("uploader"),
                            "title": entry.get("title")
                        })
                        new_downloads += 1
                    
                    if i % 10 == 0:
                        await ctx.send(f"⏳ Processed {i}/{len(entries)} videos...")
                        
                except Exception as e:
                    failed_downloads += 1
                    print(f"Failed to update {entry.get('title', 'Unknown')}: {e}")
                    continue
            
            removed_count = len(old_tracks) - (len(downloaded_tracks) - new_downloads)
            result_msg = f"✅ Updated **{playlist_name}**!\n"
            result_msg += f"📊 **{new_downloads}** new videos downloaded\n"
            if removed_count > 0:
                result_msg += f"🗑️ **{removed_count}** videos removed from playlist\n"
            if failed_downloads > 0:
                result_msg += f"⚠️ **{failed_downloads}** videos failed to update"
        else:
            raise RuntimeError("Unsupported playlist URL type for updating.")

        # Save updated playlist
        playlist_manager.save_playlist(
            playlist_data["name"], 
            downloaded_tracks, 
            ctx.guild.id, 
            playlist_url, 
            source_type
        )
        
        await ctx.reply(result_msg)

    except Exception as e:
        await ctx.reply(f"❌ Error updating playlist: {e}")

@bot.command(usage="/play <playlist_name>", help="Play a downloaded playlist.")
async def play(ctx, *, playlist_name: str):
    """Play a previously downloaded playlist."""
    # Load the playlist first
    playlist_data = playlist_manager.load_playlist(playlist_name)
    if not playlist_data:
        await ctx.reply(f"❌ Playlist **{playlist_name}** not found. Use `/playlists` to see available playlists.")
        return

    tracks = playlist_data.get("tracks", [])
    if not tracks:
        await ctx.reply(f"❌ Playlist **{playlist_name}** is empty.")
        return

    # Verify files still exist and extract paths
    existing_tracks = []
    for track in tracks:
        if isinstance(track, dict):
            track_path = track.get("path", "")
        else:
            # Handle old format (direct string paths)
            track_path = track
            
        if os.path.exists(track_path):
            existing_tracks.append(track_path)

    if not existing_tracks:
        await ctx.reply(f"❌ No files found for playlist **{playlist_name}**. Files may have been deleted.")
        return

    # Connect to voice after validation
    try:
        vc = await ensure_voice(ctx)
    except commands.CommandError as e:
        await ctx.reply(f"❌ {e}")
        return

    st = get_music_state(ctx.guild)

    # Add to queue
    for track in existing_tracks:
        await st.queue.put(track)

    # Start playing
    try:
        await st.start(vc)
    except Exception as e:
        await ctx.reply(f"❌ Failed to start playback: {e}")
        return

    if len(existing_tracks) != len(tracks):
        missing = len(tracks) - len(existing_tracks)
        await ctx.reply(f"▶️ Playing **{playlist_name}** ({len(existing_tracks)} tracks, {missing} missing files)")
    else:
        await ctx.reply(f"▶️ Playing **{playlist_name}** ({len(existing_tracks)} tracks)")

@bot.command(help="List all downloaded playlists.")
async def playlists(ctx):
    """List all available playlists."""
    available_playlists = playlist_manager.list_playlists()
    
    if not available_playlists:
        await ctx.reply("No playlists found. Use `/download <url>` to download a playlist first.")
        return
    
    playlist_info = []
    for playlist in available_playlists:
        source_icon = "🎵" if playlist["source_type"] == "spotify" else "📺" if playlist["source_type"] == "youtube" else "📁"
        update_status = "🔄" if playlist["has_url"] else "❌"
        playlist_info.append(f"{source_icon} **{playlist['name']}** ({playlist['track_count']} tracks) {update_status}")
    
    embed = discord.Embed(title="📋 Available Playlists", color=0x1DB954)
    embed.description = "\n".join(playlist_info)
    embed.add_field(name="Legend", value="🎵 Spotify | 📺 YouTube | 📁 Other\n🔄 Can update | ❌ No update URL", inline=False)
    
    await ctx.reply(embed=embed)

@bot.command(usage="/remove <playlist_name>", help="Delete a downloaded playlist.")
async def remove(ctx, *, playlist_name: str):
    """Remove a playlist (does not delete the audio files)."""
    if playlist_manager.delete_playlist(playlist_name):
        await ctx.reply(f"🗑️ Removed playlist **{playlist_name}**.")
    else:
        await ctx.reply(f"❌ Playlist **{playlist_name}** not found.")

@bot.command(usage="/info <playlist_name>", help="Show detailed information about a playlist.")
async def info(ctx, *, playlist_name: str):
    """Show detailed information about a playlist."""
    playlist_data = playlist_manager.load_playlist(playlist_name)
    if not playlist_data:
        await ctx.reply(f"❌ Playlist **{playlist_name}** not found.")
        return
    
    # Count existing vs missing files
    tracks = playlist_data.get("tracks", [])
    existing_count = 0
    missing_files = []
    
    for track in tracks:
        if isinstance(track, dict):
            track_path = track.get("path", "")
            track_name = track.get("name", "Unknown")
        else:
            track_path = track
            track_name = os.path.basename(track_path)
        
        if os.path.exists(track_path):
            existing_count += 1
        else:
            missing_files.append(track_name)
    
    embed = discord.Embed(title=f"📋 {playlist_data.get('name', 'Unknown')}", color=0x1DB954)
    
    # Basic info
    embed.add_field(name="Total Tracks", value=playlist_data.get('track_count', 0), inline=True)
    embed.add_field(name="Available", value=existing_count, inline=True)
    embed.add_field(name="Missing", value=len(missing_files), inline=True)
    
    source_type = playlist_data.get('source_type', 'unknown')
    source_icon = "🎵" if source_type == "spotify" else "📺" if source_type == "youtube" else "📁"
    embed.add_field(name="Source", value=f"{source_icon} {source_type.title()}", inline=True)
    
    can_update = bool(playlist_data.get('playlist_url'))
    embed.add_field(name="Can Update", value="✅ Yes" if can_update else "❌ No", inline=True)
    embed.add_field(name="Guild ID", value=playlist_data.get('guild_id', 'Unknown'), inline=True)
    
    # Show some missing files if any
    if missing_files and len(missing_files) <= 5:
        embed.add_field(name="Missing Files", value="\n".join(f"• {name}" for name in missing_files), inline=False)
    elif missing_files:
        embed.add_field(name="Missing Files", value=f"• {missing_files[0]}\n• {missing_files[1]}\n• {missing_files[2]}\n... and {len(missing_files) - 3} more", inline=False)
    
    if playlist_data.get('playlist_url'):
        embed.add_field(name="Original URL", value=f"[Click here]({playlist_data['playlist_url']})", inline=False)
    
    await ctx.reply(embed=embed)

@bot.command(help="Skip the current song.")
async def skip(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await ctx.reply("⏭️ Skipped.")
    else:
        await ctx.reply("Nothing is playing.")

@bot.command(help="Pause playback.")
async def pause(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.reply("⏸️ Paused.")
    else:
        await ctx.reply("Nothing is playing.")

@bot.command(help="Resume playback.")
async def resume(ctx):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.reply("▶️ Resumed.")
    else:
        await ctx.reply("Nothing is paused.")

@bot.command(help="Stop playback and clear the queue.")
async def stop(ctx):
    vc = ctx.voice_client
    if not vc:
        await ctx.reply("Not connected.")
        return
    st = get_music_state(ctx.guild)
    st.clear()
    st.stop_player()
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    await ctx.reply("⏹️ Stopped and cleared the queue.")

@bot.command(help="Show the now playing track.")
async def now(ctx):
    st = get_music_state(ctx.guild)
    if st.now_playing:
        await ctx.reply(f"🎶 Now playing: **{st.now_playing}**")
    else:
        await ctx.reply("Nothing is playing.")

@bot.command(help="Test voice connection and system info.")
async def test(ctx):
    """Simple voice connection test."""
    try:
        vc = await ensure_voice(ctx)
        await ctx.reply(f"✅ Successfully connected to **{vc.channel.name}**!")
        
        # Optionally disconnect after test
        await asyncio.sleep(2)
        await vc.disconnect()
        await ctx.send("🔌 Disconnected after test.")
        
    except commands.CommandError as e:
        await ctx.reply(f"❌ Voice test failed: {e}")
    except Exception as e:
        await ctx.reply(f"❌ Unexpected error: {e}")

@bot.command(help="Show how many items are queued.")
async def queue(ctx):
    st = get_music_state(ctx.guild)
    size = st.queue.qsize()
    if size == 0:
        await ctx.reply("Queue is empty.")
    else:
        await ctx.reply(f"{size} track(s) in queue.")

# ========= Run =========
bot.run(TOKEN)