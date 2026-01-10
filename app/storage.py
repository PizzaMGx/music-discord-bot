import json
import os
from typing import Dict, List, Optional

from config import SONGS_FILE
from utils import sanitize_filename


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


def load_songs() -> List[Dict]:
    if not os.path.exists(SONGS_FILE):
        return []
    try:
        with open(SONGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("songs", [])
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, IOError):
        return []


def save_songs(songs: List[Dict]) -> None:
    with open(SONGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"songs": songs}, f, indent=2, ensure_ascii=False)


def upsert_song(song: Dict) -> Dict:
    songs = load_songs()
    song_id = song.get("id")
    if song_id:
        for idx, existing in enumerate(songs):
            if existing.get("id") == song_id:
                songs[idx] = song
                save_songs(songs)
                return song
    songs.append(song)
    save_songs(songs)
    return song


def find_song_by_id(song_id: str) -> Optional[Dict]:
    if not song_id:
        return None
    for song in load_songs():
        if song.get("id") == song_id:
            return song
    return None
