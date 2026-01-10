import asyncio
import os
import random
import subprocess
import sys
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from player import enqueue_tracks, ensure_voice, get_music_state, rebuild_queue_from_index
from sources import (
    YDL_OPTS_DOWNLOAD_SINGLE,
    download_if_needed_for_entry,
    ensure_download_for_spotify_track,
    get_spotify_playlist_info,
    is_spotify_playlist,
    is_youtube_url,
    yt_get_single_entry,
    yt_resolve_playlist_or_video,
)
from storage import PlaylistManager, find_song_by_id, load_songs, upsert_song
from utils import normalize_track_entry, sanitize_filename, send_lines_interaction

# ========= Configuration =========
if not config.TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN env var.")
if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
    print("[warn] Spotify credentials not set. Spotify links will not work.")

config.ensure_dirs()

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
bot.remove_command("help")
playlist_manager = PlaylistManager(config.PLAYLISTS_DIR)

async def respond(interaction: discord.Interaction, content: Optional[str] = None, embed: Optional[discord.Embed] = None) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed)
    else:
        await interaction.response.send_message(content=content, embed=embed)

def ensure_track_order(st) -> None:
    if not st.track_order and st.queue.qsize() > 0:
        st.track_order = [normalize_track_entry(item) for item in list(st.queue._queue)]
        st.next_index = 0

# ========= Commands =========
@bot.event
async def on_ready():
    if config.GUILD_ID:
        guild = discord.Object(id=int(config.GUILD_ID))
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    print(f"Logged in as {bot.user}.")

@bot.tree.command(name="help", description="Show all available commands.")
async def help_command(interaction: discord.Interaction):
    commands_list = []
    for cmd in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        desc = cmd.description or "No description."
        commands_list.append(f"/{cmd.name}: {desc}")
        if getattr(cmd, "parameters", None):
            for param in cmd.parameters:
                label = f"<{param.name}>" if param.required else f"[{param.name}]"
                commands_list.append(f"    {label} [input]")

    if not commands_list:
        await respond(interaction, "No commands available.")
        return

    await send_lines_interaction(interaction, commands_list, header="Available commands:")

@bot.command(name="help", help="Show all available commands.")
async def help_prefix(ctx):
    await ctx.reply("Use /help (slash command) to see all available commands.")

# ========= Application Commands =========
@bot.tree.command(name="download", description="Download a Spotify playlist or YouTube link.")
@app_commands.describe(url="Spotify playlist or YouTube link", playlist_name="Optional name for the saved playlist")
async def download_slash(interaction: discord.Interaction, url: str, playlist_name: Optional[str] = None):
    await respond(interaction, "Starting download...")

    downloaded_tracks: List[Dict] = []
    actual_playlist_name: str = playlist_name or "Unknown Playlist"
    source_type: str = "unknown"

    try:
        if is_spotify_playlist(url):
            if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
                raise RuntimeError("Spotify credentials missing; cannot process Spotify playlists.")

            source_type = "spotify"
            actual_playlist_name, tracks = await asyncio.to_thread(get_spotify_playlist_info, url)
            if playlist_name:
                actual_playlist_name = playlist_name

            if not tracks:
                raise RuntimeError("Playlist has no tracks or could not be read.")

            await interaction.followup.send(f"Downloading **{len(tracks)}** tracks from **{actual_playlist_name}**...")

            failed_downloads = 0
            for i, (artist, title) in enumerate(tracks, 1):
                try:
                    path = await asyncio.to_thread(ensure_download_for_spotify_track, artist, title)
                    downloaded_tracks.append({
                        "name": f"{artist} - {title}",
                        "path": path,
                        "artist": artist,
                        "title": title,
                    })
                    if i % 5 == 0:
                        await interaction.followup.send(f"Downloaded {i}/{len(tracks)} tracks...")
                except Exception as e:
                    failed_downloads += 1
                    print(f"Failed to download {artist} - {title}: {e}")
                    continue

            if failed_downloads > 0:
                await interaction.followup.send(f"Failed to download {failed_downloads} tracks.")

        elif is_youtube_url(url):
            source_type = "youtube"
            actual_playlist_name, entries = await asyncio.to_thread(yt_resolve_playlist_or_video, url)
            if playlist_name:
                actual_playlist_name = playlist_name

            if not entries:
                raise RuntimeError("Could not extract any entries from the YouTube URL.")

            await interaction.followup.send(f"Downloading **{len(entries)}** videos from **{actual_playlist_name}**...")

            failed_downloads = 0
            for i, entry in enumerate(entries, 1):
                try:
                    path = await asyncio.to_thread(download_if_needed_for_entry, entry)
                    downloaded_tracks.append({
                        "name": entry.get("title", "Unknown"),
                        "path": path,
                        "artist": entry.get("uploader"),
                        "title": entry.get("title"),
                    })
                    if i % 5 == 0:
                        await interaction.followup.send(f"Downloaded {i}/{len(entries)} videos...")
                except Exception as e:
                    failed_downloads += 1
                    print(f"Failed to download {entry.get('title', 'Unknown')}: {e}")
                    continue

            if failed_downloads > 0:
                await interaction.followup.send(f"Failed to download {failed_downloads} videos.")
        else:
            raise RuntimeError("Provide a Spotify playlist or YouTube link.")

    except Exception as e:
        await respond(interaction, f"Error: {e}")
        return

    if not downloaded_tracks:
        await respond(interaction, "No tracks were successfully downloaded.")
        return

    playlist_manager.save_playlist(actual_playlist_name, downloaded_tracks, interaction.guild.id, url, source_type)
    await respond(interaction, f"Downloaded and saved **{len(downloaded_tracks)}** tracks as playlist **{actual_playlist_name}**!")


@bot.tree.command(name="downloadsong", description="Download a single YouTube song.")
@app_commands.describe(url="YouTube link")
async def downloadsong_slash(interaction: discord.Interaction, url: str):
    if not is_youtube_url(url):
        await respond(interaction, "Provide a YouTube link.")
        return

    await respond(interaction, "Starting song download...")
    try:
        entry = await asyncio.to_thread(yt_get_single_entry, url)
        path = await asyncio.to_thread(download_if_needed_for_entry, entry, YDL_OPTS_DOWNLOAD_SINGLE)
    except Exception as e:
        await respond(interaction, f"Error: {e}")
        return

    song_id = entry.get("id") or sanitize_filename(entry.get("title", "song"))
    song = {
        "id": song_id,
        "name": entry.get("title", "Unknown"),
        "url": entry.get("webpage_url") or url,
        "path": path,
        "uploader": entry.get("uploader"),
    }
    upsert_song(song)
    await respond(interaction, f"Saved song **{song['name']}** with ID `{song_id}`.")


@bot.tree.command(name="update", description="Update a playlist from its original URL.")
@app_commands.describe(playlist_name="Saved playlist name")
async def update_slash(interaction: discord.Interaction, playlist_name: str):
    playlist_data = playlist_manager.load_playlist(playlist_name)
    if not playlist_data:
        await respond(interaction, f"Playlist **{playlist_name}** not found.")
        return

    playlist_url = playlist_data.get("playlist_url")
    if not playlist_url:
        await respond(interaction, f"Playlist **{playlist_name}** doesn't have an original URL to update from.")
        return

    await respond(interaction, f"Updating playlist **{playlist_name}** from original source...")

    try:
        old_tracks = {track.get("name", ""): track for track in playlist_data.get("tracks", [])}
        downloaded_tracks: List[Dict] = []
        source_type = playlist_data.get("source_type", "unknown")

        if is_spotify_playlist(playlist_url):
            if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
                raise RuntimeError("Spotify credentials missing; cannot update Spotify playlists.")

            _, tracks = await asyncio.to_thread(get_spotify_playlist_info, playlist_url)
            if not tracks:
                raise RuntimeError("Updated playlist has no tracks or could not be read.")

            await interaction.followup.send(f"Checking **{len(tracks)}** tracks for updates...")

            new_downloads = 0
            failed_downloads = 0
            for i, (artist, title) in enumerate(tracks, 1):
                track_name = f"{artist} - {title}"
                try:
                    if track_name in old_tracks and os.path.exists(old_tracks[track_name].get("path", "")):
                        downloaded_tracks.append(old_tracks[track_name])
                    else:
                        path = await asyncio.to_thread(ensure_download_for_spotify_track, artist, title)
                        downloaded_tracks.append({
                            "name": track_name,
                            "path": path,
                            "artist": artist,
                            "title": title,
                        })
                        new_downloads += 1
                    if i % 10 == 0:
                        await interaction.followup.send(f"Processed {i}/{len(tracks)} tracks...")
                except Exception as e:
                    failed_downloads += 1
                    print(f"Failed to update {artist} - {title}: {e}")
                    continue

            removed_count = len(old_tracks) - (len(downloaded_tracks) - new_downloads)
            result_msg = f"Updated **{playlist_name}**!\n"
            result_msg += f"- {new_downloads} new tracks downloaded\n"
            if removed_count > 0:
                result_msg += f"- {removed_count} tracks removed from playlist\n"
            if failed_downloads > 0:
                result_msg += f"- {failed_downloads} tracks failed to update"

        elif is_youtube_url(playlist_url):
            _, entries = await asyncio.to_thread(yt_resolve_playlist_or_video, playlist_url)
            if not entries:
                raise RuntimeError("Updated playlist has no videos or could not be read.")

            await interaction.followup.send(f"Checking **{len(entries)}** videos for updates...")

            new_downloads = 0
            failed_downloads = 0
            for i, entry in enumerate(entries, 1):
                track_name = entry.get("title", "Unknown")
                try:
                    if track_name in old_tracks and os.path.exists(old_tracks[track_name].get("path", "")):
                        downloaded_tracks.append(old_tracks[track_name])
                    else:
                        path = await asyncio.to_thread(download_if_needed_for_entry, entry)
                        downloaded_tracks.append({
                            "name": track_name,
                            "path": path,
                            "artist": entry.get("uploader"),
                            "title": entry.get("title"),
                        })
                        new_downloads += 1
                    if i % 10 == 0:
                        await interaction.followup.send(f"Processed {i}/{len(entries)} videos...")
                except Exception as e:
                    failed_downloads += 1
                    print(f"Failed to update {entry.get('title', 'Unknown')}: {e}")
                    continue

            removed_count = len(old_tracks) - (len(downloaded_tracks) - new_downloads)
            result_msg = f"Updated **{playlist_name}**!\n"
            result_msg += f"- {new_downloads} new videos downloaded\n"
            if removed_count > 0:
                result_msg += f"- {removed_count} videos removed from playlist\n"
            if failed_downloads > 0:
                result_msg += f"- {failed_downloads} videos failed to update"
        else:
            raise RuntimeError("Unsupported playlist URL type for updating.")

        playlist_manager.save_playlist(
            playlist_data["name"],
            downloaded_tracks,
            interaction.guild.id,
            playlist_url,
            source_type,
        )
        await respond(interaction, result_msg)
    except Exception as e:
        await respond(interaction, f"Error updating playlist: {e}")


@bot.tree.command(name="play", description="Play a playlist or song.")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="playlist", value="playlist"),
        app_commands.Choice(name="song", value="song"),
    ]
)
@app_commands.describe(value="Playlist name or song ID")
async def play_slash(interaction: discord.Interaction, mode: app_commands.Choice[str], value: str):
    st = get_music_state(interaction.guild)
    if mode.value == "playlist":
        playlist_name = value
        playlist_data = playlist_manager.load_playlist(playlist_name)
        if not playlist_data:
            await respond(interaction, f"Playlist **{playlist_name}** not found.")
            return

        tracks = playlist_data.get("tracks", [])
        if not tracks:
            await respond(interaction, f"Playlist **{playlist_name}** is empty.")
            return

        existing_tracks = []
        for track in tracks:
            entry = normalize_track_entry(track)
            if os.path.exists(entry.get("path", "")):
                existing_tracks.append(entry)

        if not existing_tracks:
            await respond(interaction, f"No files found for playlist **{playlist_name}**.")
            return

        try:
            vc = await ensure_voice(interaction)
        except commands.CommandError as e:
            await respond(interaction, str(e))
            return

        await enqueue_tracks(st, existing_tracks)
        try:
            await st.start(vc)
        except Exception as e:
            await respond(interaction, f"Failed to start playback: {e}")
            return

        if len(existing_tracks) != len(tracks):
            missing = len(tracks) - len(existing_tracks)
            await respond(interaction, f"Playing **{playlist_name}** ({len(existing_tracks)} tracks, {missing} missing files)")
        else:
            await respond(interaction, f"Playing **{playlist_name}** ({len(existing_tracks)} tracks)")
        return

    song = find_song_by_id(value.strip())
    if not song:
        await respond(interaction, f"Song ID **{value}** not found.")
        return

    entry = normalize_track_entry(song)
    if not os.path.exists(entry.get("path", "")):
        await respond(interaction, "Song file is missing. Try /downloadsong again.")
        return

    try:
        vc = await ensure_voice(interaction)
    except commands.CommandError as e:
        await respond(interaction, str(e))
        return

    await enqueue_tracks(st, [entry])
    try:
        await st.start(vc)
    except Exception as e:
        await respond(interaction, f"Failed to start playback: {e}")
        return

    await respond(interaction, f"Playing song **{entry['name']}**")


@bot.tree.command(name="playlists", description="List all saved playlists.")
async def playlists_slash(interaction: discord.Interaction):
    available_playlists = playlist_manager.list_playlists()
    if not available_playlists:
        await respond(interaction, "No playlists found. Use /download first.")
        return

    playlist_info = []
    for playlist in available_playlists:
        source_icon = "Spotify" if playlist["source_type"] == "spotify" else "YouTube" if playlist["source_type"] == "youtube" else "Other"
        update_status = "Can update" if playlist["has_url"] else "No update URL"
        playlist_info.append(f"{source_icon} | **{playlist['name']}** ({playlist['track_count']} tracks) | {update_status}")

    embed = discord.Embed(title="Available Playlists", color=0x1DB954)
    embed.description = "\n".join(playlist_info)
    await respond(interaction, embed=embed)


@bot.tree.command(name="remove", description="Delete a saved playlist.")
@app_commands.describe(playlist_name="Saved playlist name")
async def remove_slash(interaction: discord.Interaction, playlist_name: str):
    if playlist_manager.delete_playlist(playlist_name):
        await respond(interaction, f"Removed playlist **{playlist_name}**.")
    else:
        await respond(interaction, f"Playlist **{playlist_name}** not found.")


@bot.tree.command(name="info", description="Show detailed information about a playlist.")
@app_commands.describe(playlist_name="Saved playlist name")
async def info_slash(interaction: discord.Interaction, playlist_name: str):
    playlist_data = playlist_manager.load_playlist(playlist_name)
    if not playlist_data:
        await respond(interaction, f"Playlist **{playlist_name}** not found.")
        return

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

    embed = discord.Embed(title=f"{playlist_data.get('name', 'Unknown')}", color=0x1DB954)
    embed.add_field(name="Total Tracks", value=playlist_data.get("track_count", 0), inline=True)
    embed.add_field(name="Available", value=existing_count, inline=True)
    embed.add_field(name="Missing", value=len(missing_files), inline=True)
    source_type = playlist_data.get("source_type", "unknown")
    embed.add_field(name="Source", value=source_type.title(), inline=True)
    can_update = bool(playlist_data.get("playlist_url"))
    embed.add_field(name="Can Update", value="Yes" if can_update else "No", inline=True)
    embed.add_field(name="Guild ID", value=playlist_data.get("guild_id", "Unknown"), inline=True)
    if missing_files and len(missing_files) <= 5:
        embed.add_field(name="Missing Files", value="\n".join(missing_files), inline=False)
    elif missing_files:
        embed.add_field(name="Missing Files", value=f"{missing_files[0]}\n{missing_files[1]}\n{missing_files[2]}\n... and {len(missing_files) - 3} more", inline=False)
    if playlist_data.get("playlist_url"):
        embed.add_field(name="Original URL", value=playlist_data["playlist_url"], inline=False)

    await respond(interaction, embed=embed)


@bot.tree.command(name="skip", description="Skip the current track.")
async def skip_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.stop()
        await respond(interaction, "Skipped.")
    else:
        await respond(interaction, "Nothing is playing.")


@bot.tree.command(name="pause", description="Pause playback.")
async def pause_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.pause()
        await respond(interaction, "Paused.")
    else:
        await respond(interaction, "Nothing is playing.")


@bot.tree.command(name="resume", description="Resume playback.")
async def resume_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_paused():
        vc.resume()
        await respond(interaction, "Resumed.")
    else:
        await respond(interaction, "Nothing is paused.")


@bot.tree.command(name="stop", description="Stop playback and clear the queue.")
async def stop_slash(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if not vc:
        await respond(interaction, "Not connected.")
        return
    st = get_music_state(interaction.guild)
    st.clear()
    st.stop_player()
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    await respond(interaction, "Stopped and cleared the queue.")


@bot.tree.command(name="now", description="Show the now playing track.")
async def now_slash(interaction: discord.Interaction):
    st = get_music_state(interaction.guild)
    if st.now_playing:
        await respond(interaction, f"Now playing: **{st.now_playing}**")
    else:
        await respond(interaction, "Nothing is playing.")


@bot.tree.command(name="test", description="Test voice connection and system info.")
async def test_slash(interaction: discord.Interaction):
    await respond(interaction, "Testing voice connection...")
    try:
        vc = await ensure_voice(interaction)
        await interaction.followup.send(f"Successfully connected to **{vc.channel.name}**!")
        await asyncio.sleep(2)
        await vc.disconnect()
        await interaction.followup.send("Disconnected after test.")
    except commands.CommandError as e:
        await interaction.followup.send(f"Voice test failed: {e}")
    except Exception as e:
        await interaction.followup.send(f"Unexpected error: {e}")


@bot.tree.command(name="update-packages", description="Update yt-dlp to the latest version (admin only).")
async def update_packages_slash(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await respond(interaction, "This command is restricted to administrators.")
        return

    await respond(interaction, "Updating yt-dlp... This may take a moment.")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as e:
        await respond(interaction, f"Update failed: {e}")
        return

    if result.returncode != 0:
        err = (result.stderr or "Unknown error").strip()
        await interaction.followup.send(f"Update failed:\n{err}")
        return

    out = (result.stdout or "Update completed.").strip()
    await interaction.followup.send(f"Update finished:\n{out}")


@bot.tree.command(name="queue", description="Show the queue or jump to a track number.")
@app_commands.describe(action="Use 'play' to jump", number="Track number to jump to")
@app_commands.choices(action=[app_commands.Choice(name="play", value="play")])
async def queue_slash(interaction: discord.Interaction, action: Optional[app_commands.Choice[str]] = None, number: Optional[int] = None):
    st = get_music_state(interaction.guild)
    if action:
        if action.value != "play" or number is None:
            await respond(interaction, "Usage: /queue play <number>")
            return
        if not st.track_order:
            await respond(interaction, "Queue is empty.")
            return
        if number < 1 or number > len(st.track_order):
            await respond(interaction, f"Pick a number between 1 and {len(st.track_order)}.")
            return
        target_index = number - 1
        if st.now_playing_index == target_index:
            await respond(interaction, "Already playing that track.")
            return

        st.next_index = target_index
        await rebuild_queue_from_index(st, target_index)
        st.now_playing = None
        st.now_playing_index = None

        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

        await respond(interaction, f"Jumped to #{number}: **{st.track_order[target_index].get('name', 'Unknown')}**")
        return

    ensure_track_order(st)

    if not st.track_order:
        await respond(interaction, "Queue is empty.")
        return

    lines = []
    for idx, entry in enumerate(st.track_order, 1):
        name = entry.get("name", "Unknown")
        suffix = " (now)" if st.now_playing_index == (idx - 1) else ""
        lines.append(f"{idx}. {name}{suffix}")
    await send_lines_interaction(interaction, lines)


@bot.tree.command(name="queue-now", description="Show the currently playing track.")
async def queue_now_slash(interaction: discord.Interaction):
    st = get_music_state(interaction.guild)
    if st.now_playing:
        await respond(interaction, f"Now playing: **{st.now_playing}**")
    else:
        await respond(interaction, "Nothing is playing.")


@bot.tree.command(name="queue-next", description="Show the next track in the queue.")
async def queue_next_slash(interaction: discord.Interaction):
    st = get_music_state(interaction.guild)
    ensure_track_order(st)
    if st.track_order and st.next_index < len(st.track_order):
        await respond(interaction, f"Next up: **{st.track_order[st.next_index].get('name', 'Unknown')}**")
    else:
        await respond(interaction, "No next track.")


@bot.tree.command(name="queue-remove", description="Remove a track from the queue by number.")
@app_commands.describe(number="Queue number to remove")
async def queue_remove_slash(interaction: discord.Interaction, number: int):
    st = get_music_state(interaction.guild)
    ensure_track_order(st)
    if not st.track_order:
        await respond(interaction, "Queue is empty.")
        return
    if number < 1 or number > len(st.track_order):
        await respond(interaction, f"Pick a number between 1 and {len(st.track_order)}.")
        return

    idx = number - 1
    was_now_playing = st.now_playing_index == idx
    removed = st.track_order.pop(idx)

    if st.now_playing_index is not None:
        if idx < st.now_playing_index:
            st.now_playing_index -= 1
        elif idx == st.now_playing_index:
            st.now_playing_index = None
            st.now_playing = None
            st.current_index = None

    if idx < st.next_index:
        st.next_index = max(0, st.next_index - 1)

    await rebuild_queue_from_index(st, st.next_index)

    vc = interaction.guild.voice_client if interaction.guild else None
    if was_now_playing and vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()

    await respond(interaction, f"Removed #{number}: **{removed.get('name', 'Unknown')}**")


@bot.tree.command(name="queue-clear", description="Clear the queue.")
async def queue_clear_slash(interaction: discord.Interaction):
    st = get_music_state(interaction.guild)
    st.clear()
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await respond(interaction, "Queue cleared.")


@bot.tree.command(name="queue-shuffle", description="Shuffle the queue (keeps the current track).")
async def queue_shuffle_slash(interaction: discord.Interaction):
    st = get_music_state(interaction.guild)
    ensure_track_order(st)
    if not st.track_order:
        await respond(interaction, "Queue is empty.")
        return

    if st.now_playing_index is None:
        random.shuffle(st.track_order)
        st.next_index = 0
    else:
        prefix = st.track_order[: st.now_playing_index + 1]
        rest = st.track_order[st.now_playing_index + 1 :]
        random.shuffle(rest)
        st.track_order = prefix + rest
        st.next_index = st.now_playing_index + 1

    await rebuild_queue_from_index(st, st.next_index)
    await respond(interaction, "Queue shuffled.")


@bot.tree.command(name="queue-repeat", description="Set queue repeat mode.")
@app_commands.choices(mode=[
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="one", value="one"),
    app_commands.Choice(name="all", value="all"),
])
async def queue_repeat_slash(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    st = get_music_state(interaction.guild)
    st.repeat_mode = mode.value
    await respond(interaction, f"Repeat mode set to **{mode.value}**.")


@bot.tree.command(name="search", description="Search saved songs by name.")
@app_commands.describe(query="Song name to search for")
async def search_slash(interaction: discord.Interaction, query: str):
    songs = load_songs()
    if not songs:
        await respond(interaction, "No saved songs. Use /downloadsong first.")
        return
    query_lower = query.strip().lower()
    matches = [s for s in songs if query_lower in s.get("name", "").lower()]
    if not matches:
        await respond(interaction, "No matches found.")
        return

    lines = []
    for song in matches[:20]:
        name = song.get("name", "Unknown")
        url = song.get("url", "Unknown")
        song_id = song.get("id", "Unknown")
        lines.append(f"{name} | {url} | {song_id}")
    if len(matches) > 20:
        lines.append(f"... and {len(matches) - 20} more")

    await send_lines_interaction(interaction, lines)

@bot.tree.command(name="join", description="Join your current voice channel.")
async def join(interaction: discord.Interaction):
    vc = await ensure_voice(interaction)
    await respond(interaction, f"Joined **{vc.channel.name}**.")

@bot.tree.command(name="leave", description="Leave the voice channel and clear the queue.")
async def leave(interaction: discord.Interaction):
    if interaction.guild and interaction.guild.voice_client:
        st = get_music_state(interaction.guild)
        st.clear()
        st.stop_player()
        await interaction.guild.voice_client.disconnect(force=True)
        await respond(interaction, "Disconnected and cleared the queue.")
    else:
        await respond(interaction, "Not connected.")

@bot.command(usage="/download <playlist_url> [playlist_name]", help="Download a Spotify playlist or YouTube link.")
async def download(ctx, url: str, *, playlist_name: str = None):
    """Download and save a playlist for later playback."""
    await ctx.reply("⏳ Starting download...")
    
    downloaded_tracks: List[Dict] = []
    actual_playlist_name: str = playlist_name or "Unknown Playlist"
    source_type: str = "unknown"
    
    try:
        if is_spotify_playlist(url):
            if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
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

@bot.command(usage="/downloadsong <youtube_link>", help="Download a single YouTube song.")
async def downloadsong(ctx, url: str):
    """Download and save a single YouTube song entry."""
    if not is_youtube_url(url):
        await ctx.reply("Provide a YouTube link.")
        return

    await ctx.reply("Starting song download...")
    try:
        entry = await asyncio.to_thread(yt_get_single_entry, url)
        path = await asyncio.to_thread(download_if_needed_for_entry, entry, YDL_OPTS_DOWNLOAD_SINGLE)
    except Exception as e:
        await ctx.reply(f"Error: {e}")
        return

    song_id = entry.get("id") or sanitize_filename(entry.get("title", "song"))
    song = {
        "id": song_id,
        "name": entry.get("title", "Unknown"),
        "url": entry.get("webpage_url") or url,
        "path": path,
        "uploader": entry.get("uploader"),
    }
    upsert_song(song)
    await ctx.reply(f"Saved song **{song['name']}** with ID `{song_id}`.")

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
            if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
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

# @bot.command(usage="/play <playlist_name>", help="Play a downloaded playlist.")
async def play_legacy(ctx, *, playlist_name: str):
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

@bot.command(usage="/play playlist <playlist_name> | /play song <song_id>", help="Play a playlist or song.")
async def play(ctx, mode: str, *, value: str):
    """Play a downloaded playlist or a saved song by ID."""
    mode = (mode or "").strip().lower()
    if mode not in ("playlist", "song"):
        await ctx.reply("Usage: /play playlist <playlist_name> | /play song <song_id>")
        return

    st = get_music_state(ctx.guild)

    if mode == "playlist":
        playlist_name = value
        playlist_data = playlist_manager.load_playlist(playlist_name)
        if not playlist_data:
            await ctx.reply(f"Г?O Playlist **{playlist_name}** not found. Use `/playlists` to see available playlists.")
            return

        tracks = playlist_data.get("tracks", [])
        if not tracks:
            await ctx.reply(f"Г?O Playlist **{playlist_name}** is empty.")
            return

        existing_tracks = []
        for track in tracks:
            entry = normalize_track_entry(track)
            if os.path.exists(entry.get("path", "")):
                existing_tracks.append(entry)

        if not existing_tracks:
            await ctx.reply(f"Г?O No files found for playlist **{playlist_name}**. Files may have been deleted.")
            return

        # Connect to voice after validation
        try:
            vc = await ensure_voice(ctx)
        except commands.CommandError as e:
            await ctx.reply(f"Г?O {e}")
            return

        await enqueue_tracks(st, existing_tracks)

        # Start playing
        try:
            await st.start(vc)
        except Exception as e:
            await ctx.reply(f"Г?O Failed to start playback: {e}")
            return

        if len(existing_tracks) != len(tracks):
            missing = len(tracks) - len(existing_tracks)
            await ctx.reply(f"Г-Л,? Playing **{playlist_name}** ({len(existing_tracks)} tracks, {missing} missing files)")
        else:
            await ctx.reply(f"Г-Л,? Playing **{playlist_name}** ({len(existing_tracks)} tracks)")
        return

    song = find_song_by_id(value.strip())
    if not song:
        await ctx.reply(f"Г?O Song ID **{value}** not found. Use `/search <name>` to find songs.")
        return

    entry = normalize_track_entry(song)
    if not os.path.exists(entry.get("path", "")):
        await ctx.reply("Г?O Song file is missing. Try `/downloadsong <link>` again.")
        return

    try:
        vc = await ensure_voice(ctx)
    except commands.CommandError as e:
        await ctx.reply(f"Г?O {e}")
        return

    await enqueue_tracks(st, [entry])
    try:
        await st.start(vc)
    except Exception as e:
        await ctx.reply(f"Г?O Failed to start playback: {e}")
        return

    await ctx.reply(f"Г-Л,? Playing song **{entry['name']}**")

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

@bot.command(name="update-packages", help="Update yt-dlp to the latest version.")
async def update_packages(ctx):
    if not ctx.author.guild_permissions.administrator:
        await ctx.reply("This command is restricted to administrators.")
        return

    await ctx.reply("Updating yt-dlp... This may take a moment.")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as e:
        await ctx.reply(f"Update failed: {e}")
        return

    if result.returncode != 0:
        err = (result.stderr or "Unknown error").strip()
        await ctx.reply(f"Update failed:\n{err}")
        return

    out = (result.stdout or "Update completed.").strip()
    await ctx.reply(f"Update finished:\n{out}")

@bot.command(help="Show the queue or jump to a track number.")
async def queue(ctx, action: str = None, number: int = None):
    st = get_music_state(ctx.guild)
    if action:
        action = action.lower()
        if action != "play" or number is None:
            await ctx.reply("Usage: /queue play <number>")
            return
        if not st.track_order:
            await ctx.reply("Queue is empty.")
            return
        if number < 1 or number > len(st.track_order):
            await ctx.reply(f"Pick a number between 1 and {len(st.track_order)}.")
            return
        target_index = number - 1
        if st.now_playing_index == target_index:
            await ctx.reply("Already playing that track.")
            return

        st.next_index = target_index
        await rebuild_queue_from_index(st, target_index)
        st.now_playing = None
        st.now_playing_index = None

        vc = ctx.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

        await ctx.reply(f"Jumped to #{number}: **{st.track_order[target_index].get('name', 'Unknown')}**")
        return

    if not st.track_order and st.queue.qsize() > 0:
        st.track_order = [normalize_track_entry(item) for item in list(st.queue._queue)]
        st.next_index = 0

    if not st.track_order:
        await ctx.reply("Queue is empty.")
        return

    lines = []
    for idx, entry in enumerate(st.track_order, 1):
        name = entry.get("name", "Unknown")
        suffix = " (now)" if st.now_playing_index == (idx - 1) else ""
        lines.append(f"{idx}. {name}{suffix}")
    await send_lines(ctx, lines)

@bot.command(usage="/search <song name>", help="Search saved songs by name.")
async def search(ctx, *, query: str):
    songs = load_songs()
    if not songs:
        await ctx.reply("No saved songs. Use `/downloadsong <link>` first.")
        return
    query_lower = query.strip().lower()
    matches = [s for s in songs if query_lower in s.get("name", "").lower()]
    if not matches:
        await ctx.reply("No matches found.")
        return

    lines = []
    for song in matches[:20]:
        name = song.get("name", "Unknown")
        url = song.get("url", "Unknown")
        song_id = song.get("id", "Unknown")
        lines.append(f"{name} | {url} | {song_id}")

    if len(matches) > 20:
        lines.append(f"... and {len(matches) - 20} more")
    await ctx.reply("\n".join(lines))

# ========= Run =========
bot.run(config.TOKEN)
