# castVideoOnly

Send **video-only** to a Chromecast while keeping audio on your computer —
ideal for routing audio through Bluetooth headphones/speakers while using a
TV or projector as the display.

The Chromecast introduces a ~2.5 s video delay due to internal buffering.
The script compensates by delaying the local audio by the same amount.

## Requirements

- **Python 3.12+**
- **ffmpeg / ffprobe** (system packages)
- **VLC 3.0+** (with `libvlc`; `python-vlc` bindings are in the venv)
- **pychromecast** (installed in the venv)
- Chromecast v2 (or any Google Cast device) on the **same network**

## Setup

```bash
git clone <this-repo> castVideoOnly
cd castVideoOnly
python3 -m venv .venv
source .venv/bin/activate
pip install pychromecast python-vlc
deactivate          # script auto-detects the venv; no need to activate to run
```

## Usage

```bash
python cast_video.py /path/to/video.mp4
```

### Arguments

| Argument | Description |
|----------|-------------|
| `video_path` | Path to the video file (required) |
| `-n NAME` | Chromecast device name (default: `Remoto`) |
| `-d DELAY` | Audio delay in milliseconds (default: `2500`) |
| `--fast` | Skip transcoding — instantly remux to MP4 (may not play on CC v2) |
| `--software` | Force software encoding (slower, but avoids VAAPI issues) |
| `--ip IP` | Manual LAN IP for the HTTP server (auto-detected if not set) |
| `-t SEC\|HH:MM:SS` | Start position (default: `0`). Examples: `-t 300` `-t 00:05:00` `-t 01:20:15` |

### Examples

```bash
# Default name "Remoto" with default 2500 ms delay
python cast_video.py ~/Videos/movie.mp4

# Specify a different Chromecast
python cast_video.py ~/Videos/movie.mp4 -n "Living Room TV"

# Start at 5 minutes
python cast_video.py ~/Videos/movie.mp4 -t 00:05:00

# Faster start (no transcoding — video may not play on CC v2)
python cast_video.py ~/Videos/movie.mp4 --fast

# Force software encoding if hardware (VAAPI) output causes black screen
python cast_video.py ~/Videos/movie.mp4 --software

# Specify LAN IP manually (useful when VPN interferes)
python cast_video.py ~/Videos/movie.mp4 --ip 192.168.0.14
```

## How it works (step by step)

1. **Video probe** — `ffprobe` reads the codec, resolution, and container. Warns
   if the format isn't natively supported by Chromecast v2.

2. **File preparation** — If the codec isn't H.264/VP8/MPEG-4, `ffmpeg`
   transcodes to H.264 using hardware encoding (VAAPI on Intel iGPU) or
   software `libx264` (`--software`). A silent AAC audio track is added
   (Chromecast default receiver may refuse video-only streams). Progress
   percentage is shown. The `+faststart` flag places the moov atom at the
   front so the Chromecast can seek.

3. **Local HTTP server** — A threaded HTTP/1.1 server on port `8800` serves
   the file with Range-request support (required by Chromecast for seeking).

4. **Chromecast casting** — `pychromecast` discovers the device by name and
   loads the video URL (`http://<LAN_IP>:8800/tmpXXXXXX.mp4`) via the default
   media receiver.

5. **Local audio playback** — VLC plays the original file in audio-only mode
   (`--no-video`). The delay is applied via `audio_set_delay()`, matching the
   Chromecast's buffering delay.

6. **Interactive control** — A curses-based terminal UI shows positions and
   accepts keyboard commands.

## Interactive Controls

| Key | Action |
|-----|--------|
| `Space` | Pause / Resume (both Chromecast + local audio) |
| `←` | Seek backward 5 seconds **(broken — see TODO)** |
| `→` | Seek forward 5 seconds **(broken — see TODO)** |
| `q` | Stop playback and clean up |

The UI displays:
- Current Chromecast video position
- Current audio position / total audio length
- Play/pause status

## Audio Routing to Bluetooth

No script changes needed. The audio plays through your system's current output.
To use Bluetooth:

1. Connect your Bluetooth speaker/headphones via system settings.
2. Set it as the default audio sink (or configure your audio server to route
   VLC to it).
3. Run the script normally — VLC will output to the active audio device.

## Tuning the Delay

The Chromecast's video delay varies depending on your network and bitrate.

- **Audio heard before video** → increase delay (`-d 3000`, `-d 3500`...)
- **Audio heard after video** → decrease delay (`-d 2000`, `-d 1500`...)

Start with the default 2500 ms and adjust in 250–500 ms steps.

## Notes

- The script names its Chromecast target **"Remoto"** (change with `-n` or edit
  `DEFAULT_NAME` in the script).
- Temp files in `/tmp/tmp*.mp4` older than 30 days are automatically cleaned
  on each run.
- When the script exits, it tells the Chromecast to quit the receiver app,
  stops VLC, shuts down the HTTP server, and deletes the temp file.

## Troubleshooting

| Problem | Likely fix |
|---------|------------|
| `Chromecast 'Remoto' not found` | Check Chromecast is on, same network, and name is correct. |
| `Port 8800 is already in use` | Kill the process using that port. |
| `ffmpeg failed` | Ensure `ffmpeg` is installed (`ffmpeg -version`). |
| Black screen on Chromecast | The file may not be compatible. Try `--software` or `--fast`. If on a VPN, pass `--ip <LAN_IP>`. |
| Audio out of sync | Adjust with `-d`. Default is 2500 ms. |
| Seeking jumps to beginning | See TODO below — seek is currently broken. |
| `Could not detect LAN IP` | Pass `--ip <LAN_IP>` manually (VPN may interfere with auto-detection). |

## TODO

- **Seeking** (`←` / `→`) does not work — the Chromecast ignores the position
  command and restarts the video from the beginning. Audio seeks correctly.
  Need a reliable seek mechanism.

## Logs

All errors (including full ffmpeg output) are logged to **`cast.log`** in the
project directory. When a failure occurs:

1. The terminal shows a brief error and "Press any key to exit"
2. The full error details are in `cast.log`
3. After exiting, the script prints the log path

You can also tail the log during a run:
```bash
tail -f cast.log
```

## Project Structure

```
castVideoOnly/
├── .venv/           ← Python virtual environment
├── cast_video.py    ← Main script
├── cast.log         ← Runtime log (created automatically)
└── README.md        ← This file
```
