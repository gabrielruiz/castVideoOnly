# castVideoOnly

Send **video-only** to a Chromecast while keeping audio on your computer — ideal for routing audio through Bluetooth headphones/speakers connected to your machine while using a TV or projector as the display.

The Chromecast introduces a ~2 s video delay due to internal buffering. The script compensates by delaying the local audio by the same amount, keeping everything in sync.

## Requirements

- **Python 3.12+**
- **ffmpeg / ffplay** (system packages)
- **VLC 3.0+** (with `libvlc`; `python-vlc` bindings are installed in the venv)
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
| `-n NAME` | Chromecast device name (default: `Remote`) |
| `-d DELAY` | Audio delay in milliseconds (default: `2500`) |

### Examples

```bash
# Use default name "Remote" with default 2500ms delay
python cast_video.py ~/Videos/movie.mp4

# Specify a different Chromecast
python cast_video.py ~/Videos/movie.mp4 -n "Living Room TV"

# Adjust sync manually (+500ms if audio still lags)
python cast_video.py ~/Videos/movie.mp4 -d 3000

# Reduce delay if audio is ahead of video
python cast_video.py ~/Videos/movie.mp4 -d 2000
```

## How it works (step by step)

1. **Video extraction** — `ffmpeg` stream-copies the video track and strips audio (`-an -c:v copy`). No re-encoding, so it's near-instant and lossless.

2. **Local HTTP server** — A lightweight Python `http.server` on port `8800` serves the video-only file with Range-request support (required by Chromecast for seeking).

3. **Chromecast casting** — `pychromecast` discovers your device by name and sends the video URL (`http://<LAN_IP>:8800/video_only.mp4`).

4. **Local audio playback** — VLC plays the original audio track in audio-only mode (`--no-video`). The delay (`DEFAULT_DELAY`, 2500 ms) is applied via `audio_set_delay()`, matching the Chromecast's buffering delay.

5. **Interactive control** — A curses-based terminal UI lets you control both players in lockstep.

## Interactive Controls

Once the script is running, the terminal shows playback status and accepts keyboard commands:

| Key | Action |
|-----|--------|
| `Space` | Pause / Resume (both Chromecast + local audio) |
| `←` | Seek backward 5 seconds |
| `→` | Seek forward 5 seconds |
| `q` | Stop playback and clean up |

The UI displays:
- Current Chromecast video position
- Current audio position (minus the delay offset) / total audio length
- Play/pause status

## Audio Routing to Bluetooth

No script changes needed. The audio plays through your system's current audio output. To use Bluetooth:

1. Connect your Bluetooth speaker/headphones via your system Bluetooth settings.
2. Set it as the default audio sink (or configure your audio server to route VLC to it).
3. Run the script normally — VLC will output to the active audio device.

On PipeWire/PulseAudio systems you can also move the stream to a specific device on the fly with `pavucontrol` or `pw-top`.

## Tuning the Delay

The Chromecast's video delay varies depending on your network and the video bitrate. If audio and video drift apart:

- **Audio heard before video** → increase delay (`-d 3000`, `-d 3500`...)
- **Audio heard after video** → decrease delay (`-d 2000`, `-d 1500`...)

Start with the default 2500 ms and adjust in 250–500 ms steps until it feels right.

## Troubleshooting

| Problem | Likely fix |
|---------|------------|
| `Chromecast 'Remote' not found` | Check Chromecast is on, same network, and name is correct. |
| `Port 8800 is already in use` | Kill the process using that port or change to a different port in `cast_video.py`. |
| `ffmpeg failed` | Ensure `ffmpeg` is installed (`ffmpeg -version`). The file format may not be supported. |
| Audio desyncs after seeking | Chromecast rebuffering varies. Try pausing briefly after seeking to let it stabilize. |

## TODO

- **Seeking** (←/→) does not work properly — the Chromecast ignores the seek
  command and restarts the video from the beginning. Audio seeks correctly.
  Fix this before the next release.

## Logs

All errors (including full ffmpeg output) are logged to **`cast.log`** in the project directory. When a failure occurs:

1. The terminal shows a brief error and "Press any key to exit"
2. The full error details are in `cast.log`
3. After exiting, the script prints the log path

You can also tail the log during a run:
```bash
tail -f cast.log
```

## Cleanup

When you press `q` or the video ends, the script:
- Stops Chromecast playback
- Stops the local audio player
- Shuts down the HTTP server
- Deletes the temporary video-only file

## Project Structure

```
castVideoOnly/
├── .venv/           ← Python virtual environment
├── cast_video.py    ← Main script
└── README.md        ← This file
```
