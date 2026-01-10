import os
import re
from typing import Dict, List, Optional


def sanitize_filename(name: str) -> str:
    # keep it short & filesystem-safe
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150]


def make_track_filename(download_dir: str, artist: str, title: str) -> str:
    safe = sanitize_filename(f"{artist} - {title}")
    return os.path.join(download_dir, f"{safe}.webm")


def normalize_track_entry(track) -> Dict:
    if isinstance(track, dict):
        path = track.get("path", "")
        name = track.get("name") or os.path.basename(path)
        entry = {**track, "name": name, "path": path}
        return entry
    path = track
    return {"name": os.path.basename(path), "path": path}


async def send_lines(ctx, lines: List[str], header: Optional[str] = None) -> None:
    chunks: List[str] = []
    current = f"{header}\n" if header else ""
    for line in lines:
        extra = f"{line}\n"
        if len(current) + len(extra) > 1900:
            if current.strip():
                chunks.append(current.rstrip())
            current = extra
        else:
            current += extra
    if current.strip():
        chunks.append(current.rstrip())

    for idx, chunk in enumerate(chunks):
        if idx == 0:
            await ctx.reply(chunk)
        else:
            await ctx.send(chunk)


async def send_lines_interaction(interaction, lines: List[str], header: Optional[str] = None) -> None:
    chunks: List[str] = []
    current = f"{header}\n" if header else ""
    for line in lines:
        extra = f"{line}\n"
        if len(current) + len(extra) > 1900:
            if current.strip():
                chunks.append(current.rstrip())
            current = extra
        else:
            current += extra
    if current.strip():
        chunks.append(current.rstrip())

    for idx, chunk in enumerate(chunks):
        if idx == 0:
            if interaction.response.is_done():
                await interaction.followup.send(chunk)
            else:
                await interaction.response.send_message(chunk)
        else:
            await interaction.followup.send(chunk)
