import asyncio
import os
from typing import Dict, List, Optional

import discord
from discord.ext import commands

FFMPEG_OPTIONS = {
    "options": "-vn -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
}

VOICE_DAVE_ERROR = (
    "Discord rejected the voice connection because this channel requires DAVE/E2EE support "
    "(voice close code 4017). Rebuild the container so it installs discord.py 2.7+ and davey."
)


class GuildMusic:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: asyncio.Queue[Dict] = asyncio.Queue()
        self.track_order: List[Dict] = []
        self.next_index: int = 0
        self.now_playing_index: Optional[int] = None
        self.current_index: Optional[int] = None
        self.now_playing: Optional[str] = None
        self.player_task: Optional[asyncio.Task] = None
        self.stop_event = asyncio.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.repeat_mode: str = "off"  # off | one | all

    async def start(self, voice_client: discord.VoiceClient):
        if self.player_task and not self.player_task.done():
            return
        self.stop_event.clear()
        self.loop = asyncio.get_running_loop()
        self.player_task = asyncio.create_task(self._player_loop(voice_client))

    async def _player_loop(self, vc: discord.VoiceClient):
        while not self.stop_event.is_set():
            try:
                entry = await asyncio.wait_for(self.queue.get(), timeout=1800)  # 30 min idle timeout
            except asyncio.TimeoutError:
                if vc and vc.is_connected():
                    await vc.disconnect(force=True)
                break

            # Check if voice client is still connected
            if not vc or not vc.is_connected():
                print("[player] Voice client disconnected, stopping player")
                break

            if self.track_order:
                self.current_index = self.next_index
                self.now_playing_index = self.current_index
                self.next_index = self.current_index + 1
            else:
                self.now_playing_index = None
                self.current_index = None

            path = entry.get("path") if isinstance(entry, dict) else entry
            name = entry.get("name") if isinstance(entry, dict) else None
            self.now_playing = name or os.path.basename(path)
            
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
                    if self.loop:
                        self.loop.call_soon_threadsafe(play_finished.set)

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

            if self.track_order and self.current_index is not None:
                if self.repeat_mode == "one":
                    self.next_index = self.current_index
                    self.clear_pending_queue()
                    await self.queue.put(entry)
                    for rest in self.track_order[self.current_index + 1:]:
                        await self.queue.put(rest)
                elif self.repeat_mode == "all" and self.current_index >= len(self.track_order) - 1:
                    self.next_index = 0
                    self.clear_pending_queue()
                    for rest in self.track_order:
                        await self.queue.put(rest)
            
            # Small delay between tracks
            await asyncio.sleep(0.5)

    def clear(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break
        self.track_order = []
        self.next_index = 0
        self.now_playing_index = None
        self.now_playing = None

    def clear_pending_queue(self):
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                break

    def stop_player(self):
        self.stop_event.set()


MUSIC: dict[int, GuildMusic] = {}


def get_music_state(guild: discord.Guild) -> GuildMusic:
    st = MUSIC.get(guild.id)
    if not st:
        st = GuildMusic(guild)
        MUSIC[guild.id] = st
    return st


async def ensure_voice(interaction) -> discord.VoiceClient:
    user = interaction.user if hasattr(interaction, "user") else interaction.author
    if not user.voice or not user.voice.channel:
        raise commands.CommandError("You must be **in a voice channel**.")
    
    channel = user.voice.channel
    guild = interaction.guild
    vc: discord.VoiceClient = guild.voice_client if guild else None
    
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
        except discord.ConnectionClosed as e:
            if getattr(e, "code", None) == 4017:
                raise commands.CommandError(VOICE_DAVE_ERROR) from e
            print(f"[voice] Connection closed on attempt {attempt + 1}: {e}")
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


async def enqueue_tracks(st: GuildMusic, entries: List[Dict], reset: bool = False) -> None:
    is_idle = st.queue.qsize() == 0 and st.now_playing is None
    if reset or not st.track_order or is_idle:
        st.track_order = list(entries)
        st.next_index = 0
        st.now_playing_index = None
        st.clear_pending_queue()
        for entry in st.track_order:
            await st.queue.put(entry)
        return

    st.track_order.extend(entries)
    for entry in entries:
        await st.queue.put(entry)


async def enqueue_next(st: GuildMusic, entry: Dict) -> int:
    """Insert a track immediately after the current track (LIFO for repeated adds)."""
    if not st.track_order or st.now_playing_index is None:
        await enqueue_tracks(st, [entry])
        return st.next_index

    insert_at = min(max(st.next_index, st.now_playing_index + 1), len(st.track_order))
    st.track_order.insert(insert_at, entry)
    await rebuild_queue_from_index(st, st.next_index)
    return insert_at


async def rebuild_queue_from_index(st: GuildMusic, start_index: int) -> None:
    st.clear_pending_queue()
    for entry in st.track_order[start_index:]:
        await st.queue.put(entry)
