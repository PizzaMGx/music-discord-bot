# Discord Music Bot

A self-hosted Discord bot that downloads audio from YouTube, saves individual songs and YouTube/Spotify playlists, and plays the local files in a Discord voice channel. It includes searchable saved songs, autocomplete, and a per-server playback queue.

> Only download and play media you have permission to use. You are responsible for complying with the source site's terms and applicable copyright law.

## Quick start with Docker Compose

### 1. Create a Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications), create an application, and add a bot.
2. On **Bot**, copy/reset the token and put it in `.env` as `DISCORD_TOKEN`.
3. Enable **Message Content Intent**. Voice state access is enabled by default.
4. On **OAuth2 > URL Generator**, select the `bot` and `applications.commands` scopes.
5. Give the bot these permissions: **View Channels**, **Send Messages**, **Connect**, and **Speak**, then use the generated URL to invite it.

Never commit or share the bot token. If it is exposed, reset it in the Developer Portal.

### 2. Configure the project

From the project directory:

```bash
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
DISCORD_TOKEN=your-token-here
```

For faster slash-command registration during development, enable Discord Developer Mode, right-click your server, copy its ID, and set:

```dotenv
DISCORD_GUILD_ID=123456789012345678
```

Spotify credentials are optional. Add `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` only if you want to import Spotify playlists. The bot reads Spotify metadata, finds each track on YouTube, and downloads that result; it does not download audio from Spotify.

### 3. Start the bot

```bash
docker compose up -d --build
docker compose logs -f bot
```

The bot is ready when the log says `Logged in as ...`. The `downloads/` and `playlists/` directories are mounted from the host, so saved music survives container rebuilds.

To stop it:

```bash
docker compose down
```

## Play or queue one song

Join a Discord voice channel first. Individual songs have a simple three-step workflow:

1. Save a YouTube song:

   ```text
   /downloadsong url:https://www.youtube.com/watch?v=VIDEO_ID
   ```

2. Find it by any part of its title, uploader, or ID:

   ```text
   /search query:song name
   ```

3. Start it or append it to the queue:

   ```text
   /play mode:song value:song name
   /queue-add song:song name
   ```

The `value` and `song` fields provide autocomplete as you type. `/play` starts playback when idle and adds the song to the end when music is already active. `/queue-add` inserts a song directly after the current track. Repeated `/queue-add` calls behave like a stack: the most recently added song plays next.

For example, if `Current` is playing and the queue is `A, B`, adding `X` and then `Y` produces `Current, Y, X, A, B`.

If a partial title matches more than one saved song, select an autocomplete result or use the exact ID shown by `/search`.

## Playlist workflow

Download and save a YouTube video/playlist:

```text
/download url:https://www.youtube.com/playlist?list=... playlist_name:Road Trip
```

Import a Spotify playlist (Spotify credentials required):

```text
/download url:https://open.spotify.com/playlist/... playlist_name:Favorites
```

Then use:

```text
/playlists
/info playlist_name:Road Trip
/play mode:playlist value:Road Trip
/update playlist_name:Road Trip
/remove playlist_name:Road Trip
```

Playlist names also autocomplete in `/play` after choosing `mode:playlist`.

## Playback and queue commands

| Command | Purpose |
| --- | --- |
| `/join` | Join your voice channel |
| `/leave` | Disconnect and clear the queue |
| `/now` or `/queue-now` | Show the current song |
| `/queue` | List the current ordered queue |
| `/queue-all` | Show the full queue with played, current, next, and upcoming status |
| `/queue play number:3` | Jump to queue item 3 |
| `/queue-next` | Show the next song |
| `/queue-add song:...` | Insert one saved song after the current track (newest plays first) |
| `/queue-remove number:3` | Remove one queue item |
| `/queue-shuffle` | Shuffle upcoming songs |
| `/queue-repeat mode:off\|one\|all` | Set repeat behavior |
| `/queue-clear` | Clear the queue and stop playback |
| `/skip` | Skip the current song |
| `/pause` and `/resume` | Pause or resume playback |
| `/stop` | Stop and clear the queue |
| `/test` | Test joining and leaving your voice channel |
| `/help` | List commands registered in Discord |

The queue and current playback state are kept in memory. Restarting the container clears the active queue, but downloaded songs and saved playlist metadata remain on disk.

## Troubleshooting

### Slash commands do not appear

- Confirm the invite used both `bot` and `applications.commands` scopes.
- Set `DISCORD_GUILD_ID` to the correct numeric server ID during development, then rebuild/restart the bot.
- Check `docker compose logs -f bot` for an invalid token, guild ID, or Discord permission error.
- Global commands can take longer to propagate when `DISCORD_GUILD_ID` is left empty.

### The bot says you must be in a voice channel

Join a normal voice channel before `/play`, `/queue-add`, `/join`, or `/test`. Run the command in the same Discord server as that voice channel.

### Voice connection fails, including close code 4017 or DAVE/E2EE errors

Rebuild instead of merely restarting so the current `discord.py`, `davey`, and voice dependencies in `requirements.txt` are installed:

```bash
docker compose build --no-cache bot
docker compose up -d bot
docker compose logs -f bot
```

Also verify the bot has **Connect** and **Speak** permissions for that specific voice channel. Run `/test` to isolate voice connection problems from downloading/playback problems.

### A YouTube download fails

YouTube changes frequently. First rebuild the image to install the current dependency version allowed by `requirements.txt`, or use `/update-packages` as a Discord server administrator and then retry. A container recreation discards an in-container package update, so rebuilding is the durable fix.

Age-restricted, private, members-only, region-blocked, live, or DRM-protected media may not be downloadable. Review the bot log for the exact `yt-dlp` error.

### Spotify links do not work

Set both `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env`, then recreate the container:

```bash
docker compose up -d --force-recreate bot
```

Spotify support only recognizes playlist URLs. Individual Spotify track URLs are not supported.

### The bot finds a song but says its file is missing

The JSON entry exists under `playlists/songs.json`, but the audio file is absent from `downloads/`. Run `/downloadsong` again with the original YouTube link. Make sure the Compose volume mappings have not been changed and both host directories are writable.

### Search returns several similarly named songs

Choose a result from autocomplete in `/play` or `/queue-add`. Alternatively, copy the exact ID displayed by `/search` and use it as the command value.

### Audio does not play or FFmpeg errors appear

The Docker image includes FFmpeg. Confirm you are running the Compose-built image, the bot has **Speak** permission, and the saved path exists inside the container:

```bash
docker compose exec bot ffmpeg -version
docker compose exec bot ls -la /downloads
```

### Useful diagnostics

```bash
docker compose ps
docker compose logs --tail=200 bot
docker compose exec bot python --version
docker compose exec bot python -m pip show discord.py davey yt-dlp
```

## Run without Docker

Docker is recommended because it supplies FFmpeg and native voice libraries. For a local run, install Python 3.11+, FFmpeg, and the native build dependencies required by PyNaCl, then:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p downloads playlists
export DISCORD_TOKEN='your-token-here'
export DOWNLOAD_DIR="$PWD/downloads"
export PLAYLISTS_DIR="$PWD/playlists"
python app/app.py
```

On Windows PowerShell, use `.venv\Scripts\Activate.ps1` and `$env:NAME = 'value'` instead of the shell commands above.

## Data layout

- `downloads/`: downloaded audio files.
- `playlists/songs.json`: searchable individual-song records created by `/downloadsong`.
- `playlists/<name>.json`: saved playlist metadata.
- `app/`: bot source code.

Back up both `downloads/` and `playlists/` to preserve the complete library.
