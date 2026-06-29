#!/usr/bin/env python3

import sys
from pathlib import Path

# Auto-detect the .venv next to this script so you don't need to
# "source .venv/bin/activate" before running it.
_venv = Path(__file__).resolve().parent / '.venv'
_ver = f'python{sys.version_info.major}.{sys.version_info.minor}'
_site = _venv / 'lib' / _ver / 'site-packages'
if _site.exists() and str(_site) not in sys.path:
    sys.path.insert(0, str(_site))

import argparse
import curses
import json
import logging
import os
import re
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pychromecast
import vlc

_LOG_PATH = Path(__file__).resolve().parent / 'cast.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s %(message)s',
    datefmt='%H:%M:%S',
    filename=_LOG_PATH,
    filemode='a',
)
log = logging.getLogger(__name__)

DEFAULT_NAME = "Remoto"
DEFAULT_DELAY = 2500
HTTP_PORT = 8800


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


def probe_video(path):
    """Return codec and container info, or None on error."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format', str(path)],
            capture_output=True, text=True, check=True
        )
        data = json.loads(r.stdout)
        streams = data.get('streams', [])
        video = next((s for s in streams if s['codec_type'] == 'video'), None)
        return {
            'codec': video['codec_name'] if video else 'unknown',
            'width': int(video.get('width', 0)) if video else 0,
            'height': int(video.get('height', 0)) if video else 0,
            'container': data.get('format', {}).get('format_name', 'unknown'),
        }
    except Exception as e:
        log.warning("Could not probe video: %s", e)
        return None

# Codecs supported by Chromecast v2 (most common)
_CC_SUPPORTED = {'h264', 'vp8', 'mpeg4', 'vc1'}

def check_compatibility(info):
    if not info:
        return None
    warnings = []
    if info['codec'].lower() not in _CC_SUPPORTED:
        warnings.append(
            f"Codec '{info['codec']}' may not play on Chromecast v2 "
            f"(supported: H.264, VP8, MPEG-4)"
        )
    if info['height'] > 1080:
        warnings.append(
            f"Resolution {info['width']}x{info['height']} exceeds "
            f"Chromecast v2 1080p limit"
        )
    if info['container'] not in ('mp4', 'mov', 'matroska'):
        warnings.append(
            f"Container '{info['container']}' may not be playable"
        )
    return warnings

def needs_transcode(info):
    if not info:
        return True
    return info['codec'].lower() not in _CC_SUPPORTED

def get_video_duration(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True
    )
    if r.returncode == 0 and r.stdout.strip():
        try:
            return float(r.stdout.strip())
        except ValueError:
            pass
    return None

_FORCE_SOFTWARE = False

def force_software_encoder(val=True):
    global _FORCE_SOFTWARE
    _FORCE_SOFTWARE = val

def detect_encoder():
    """Return (encoder_name, extra_opts) for best available H.264 encoder."""
    if _FORCE_SOFTWARE:
        return 'libx264', [
            '-pix_fmt', 'yuv420p',
            '-c:v', 'libx264', '-preset', 'fast',
            '-profile:v', 'high', '-level:v', '4.1',
        ]
    # VAAPI via Intel iGPU
    if os.path.exists('/dev/dri/renderD128'):
        try:
            r = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                               capture_output=True, text=True)
            if 'h264_vaapi' in r.stdout:
                return 'h264_vaapi', [
                    '-vaapi_device', '/dev/dri/renderD128',
                    '-vf', 'format=nv12,hwupload',
                    '-c:v', 'h264_vaapi',
                    '-profile:v', 'high', '-level:v', '4.1',
                ]
        except OSError:
            pass

    # Software fallback
    return 'libx264', [
        '-pix_fmt', 'yuv420p',
        '-c:v', 'libx264', '-preset', 'fast',
        '-profile:v', 'high', '-level:v', '4.1',
    ]


def build_cast_stream(input_path, output_path, info, stdscr, msg_line, fast=False, start_time=0):
    """Build a Chromecast-compatible file, with silent audio. Shows progress."""
    duration = get_video_duration(input_path)
    do_transcode = needs_transcode(info) and not fast

    if do_transcode:
        enc_name, enc_opts = detect_encoder()
        action = f"Transcoding ({enc_name})"
        remaining = (duration - start_time) if duration else None
        est_min = (remaining / 60 / 5) if remaining else None
        if est_min and est_min > 2:
            _msg(stdscr, msg_line, f"Estimated: ~{est_min:.0f} min — press Ctrl+C to cancel")
            time.sleep(3)
    elif fast:
        enc_opts = ['-c:v', 'copy']
        action = "Copying (fast mode — may not play)"
    else:
        enc_opts = ['-c:v', 'copy']
        action = "Remuxing"

    cmd = [
        'ffmpeg', '-y', '-loglevel', 'error',
        '-progress', '-',
    ]
    if start_time > 0:
        cmd += ['-ss', str(start_time)]
    cmd += [
        '-i', str(input_path),
        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
    ] + enc_opts + [
        '-c:a', 'aac', '-shortest',
        '-map', '0:v:0', '-map', '1:a:0',
        '-movflags', '+faststart',
        str(output_path),
    ]

    process = subprocess.Popen(cmd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,
                               universal_newlines=True)

    finalizing = False
    try:
        while True:
            r, _, _ = select.select([process.stdout], [], [], 5)
            if r:
                line = process.stdout.readline()
                if not line:
                    break
                if line.startswith('out_time='):
                    raw = line.split('=', 1)[1].strip()
                    parts = raw.replace('.', ':').split(':')
                    if len(parts) >= 4:
                        h, mi, s, ms = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3][:2])
                        current = h * 3600 + mi * 60 + s + ms / 100
                        pct = min(current / duration * 100, 99.9) if duration else 0
                        _msg(stdscr, msg_line, f"{action}... {pct:.0f}%")
                elif line.startswith('progress=end'):
                    finalizing = True
            elif process.poll() is not None:
                break
            elif finalizing:
                _msg(stdscr, msg_line, f"{action}... finalizing (may take a moment)")
            else:
                _msg(stdscr, msg_line, f"{action}... still working")
    finally:
        process.wait()

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)


def guess_mime(path):
    ext = Path(path).suffix.lower()
    return {
        '.mp4': 'video/mp4',
        '.mkv': 'video/x-matroska',
        '.webm': 'video/webm',
        '.avi': 'video/x-msvideo',
        '.mov': 'video/quicktime',
    }.get(ext, 'video/mp4')


_TEXT_SUBTITLE_CODECS = {'subrip', 'ass', 'ssa', 'text', 'webvtt'}


def extract_subtitles(video_path, tmp_dir, delay_ms=0):
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_streams', str(video_path)],
            capture_output=True, text=True, check=True
        )
        data = json.loads(r.stdout)
    except Exception as e:
        log.warning("Could not probe subtitles: %s", e)
        return []

    tracks = []
    for s in data.get('streams', []):
        if s['codec_type'] != 'subtitle':
            continue
        codec = s.get('codec_name', '')
        if codec not in _TEXT_SUBTITLE_CODECS:
            log.info("Skipping image-based subtitle track %d (%s)", s['index'], codec)
            continue
        idx = s['index']
        tags = s.get('tags', {})
        lang = tags.get('language', 'und')
        name = tags.get('title', lang.upper())
        orig_name = f"sub_{idx}.orig.vtt"
        orig_path = os.path.join(tmp_dir, orig_name)
        try:
            subprocess.run(
                ['ffmpeg', '-y', '-loglevel', 'error',
                 '-i', str(video_path),
                 '-map', f'0:{idx}',
                 str(orig_path)],
                capture_output=True, check=True
            )
            shifted_name = f"sub_{idx}.vtt"
            shifted_path = os.path.join(tmp_dir, shifted_name)
            shift_vtt_file(orig_path, shifted_path, delay_ms)
            tracks.append({
                'id': idx,
                'lang': lang,
                'name': name,
                'url_path': shifted_name,
                'orig_path': orig_path,
            })
            log.info("Extracted subtitle track %d (%s) as %s (offset %dms)", idx, name, shifted_name, delay_ms)
        except subprocess.CalledProcessError as e:
            log.warning("Failed to extract subtitle track %d: %s", idx, e)

    return tracks


def format_time(seconds):
    if seconds is None or seconds < 0:
        return "00:00:00"
    hours = int(seconds) // 3600
    minutes = (int(seconds) % 3600) // 60
    secs = int(seconds) % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

_VTT_TS_RE = re.compile(r'(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})')

def _vtt_ts_to_ms(ts):
    h, m, s_ms = ts.split(':')
    s, ms = s_ms.split('.')
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)

def _ms_to_vtt_ts(ms):
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f'{h:02d}:{m:02d}:{s:02d}.{ms:03d}'

def shift_vtt_file(src_path, dst_path, offset_ms):
    lines = []
    with open(src_path, encoding='utf-8') as f:
        for line in f:
            if _VTT_TS_RE.search(line):
                m = _VTT_TS_RE.search(line)
                start = _vtt_ts_to_ms(m.group(1))
                end = _vtt_ts_to_ms(m.group(2))
                start = max(0, start + offset_ms)
                end = max(0, end + offset_ms)
                line = f'{_ms_to_vtt_ts(start)} --> {_ms_to_vtt_ts(end)}\n'
            lines.append(line)
    # Inject X-TIMESTAMP-MAP right after the WEBVTT header
    ts_line = 'X-TIMESTAMP-MAP=MPEGTS:90000,LOCAL:00:00:00.000\n'
    if len(lines) > 0:
        first = lines[0]
        if not first.rstrip('\n\r').startswith('X-TIMESTAMP-MAP'):
            if first.rstrip('\n\r') == 'WEBVTT':
                lines.insert(1, ts_line)
            else:
                lines.insert(0, ts_line)
    if not any(l.startswith('X-TIMESTAMP-MAP') for l in lines):
        lines.insert(0, ts_line)
    with open(dst_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


def make_handler(directory):
    class Handler(SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def end_headers(self):
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Access-Control-Allow-Origin', '*')
            super().end_headers()

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except ConnectionError:
                pass

        def log_message(self, fmt, *args):
            pass

    return Handler


class CastController:
    def __init__(self, device_name):
        self.device_name = device_name
        self.cast = None
        self.mc = None

    def connect(self):
        chromecasts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=[self.device_name]
        )
        if not chromecasts:
            if browser:
                browser.stop_discovery()
            raise RuntimeError(
                f"Chromecast '{self.device_name}' not found. "
                f"Make sure it's on the same network and powered on."
            )
        self.cast = chromecasts[0]
        self.cast.wait()
        # Only stop the browser AFTER the connection is established
        if browser:
            browser.stop_discovery()
        self.mc = self.cast.media_controller

    def cast_url(self, url, content_type='video/mp4', current_time=0, subtitle_tracks=None):
        if subtitle_tracks:
            tracks_msg = []
            for t in subtitle_tracks:
                tracks_msg.append({
                    "trackId": t['id'],
                    "trackContentId": t['url'],
                    "language": t['lang'],
                    "subtype": "SUBTITLES",
                    "type": "TEXT",
                    "trackContentType": "text/vtt",
                    "name": t['name'],
                })
            self.mc.play_media(
                url, content_type,
                subtitles=tracks_msg[0]["trackContentId"],
                subtitles_lang=tracks_msg[0]["language"],
                subtitle_id=tracks_msg[0]["trackId"],
                subtitles_mime="text/vtt",
                media_info={"tracks": tracks_msg},
            )
        else:
            self.mc.play_media(url, content_type)
        self.mc.block_until_active(timeout=30)

    def reload_at(self, url, content_type, position, subtitle_tracks=None):
        try:
            self.mc.stop()
        except Exception:
            pass
        time.sleep(0.3)
        if subtitle_tracks:
            tracks_msg = []
            for t in subtitle_tracks:
                tracks_msg.append({
                    "trackId": t['id'],
                    "trackContentId": t['url'],
                    "language": t['lang'],
                    "subtype": "SUBTITLES",
                    "type": "TEXT",
                    "trackContentType": "text/vtt",
                    "name": t['name'],
                })
            self.mc.play_media(
                url, content_type,
                subtitles=tracks_msg[0]["trackContentId"],
                subtitles_lang=tracks_msg[0]["language"],
                subtitle_id=tracks_msg[0]["trackId"],
                subtitles_mime="text/vtt",
                media_info={"tracks": tracks_msg},
            )
        else:
            self.mc.play_media(url, content_type)
        self.mc.block_until_active(timeout=15)
        if position > 0:
            time.sleep(0.5)
            try:
                self.mc.seek(position)
                time.sleep(0.3)
            except Exception as e:
                log.warning("Reload seek to %.1f failed: %s", position, e)

    def update_subtitle_track(self, tracks, active_idx):
        """Update subtitle track URL (with current offset) and re-enable it."""
        try:
            track = tracks[active_idx]
            track_data = {
                "trackId": track['id'],
                "trackContentId": track['url'],
                "language": track['lang'],
                "subtype": "SUBTITLES",
                "type": "TEXT",
                "trackContentType": "text/vtt",
                "name": track['name'],
            }
            self.mc._send_command(
                {"type": "EDIT_TRACKS_INFO",
                 "activeTrackIds": [track['id']],
                 "tracks": [track_data]},
                callback_function=None,
            )
            log.info("Subtitle track %d updated with offset", track['id'])
        except Exception as e:
            log.warning("Subtitle track update failed: %s", e)

    def pause(self):
        try:
            self.mc.pause()
        except Exception as e:
            log.warning("Pause failed: %s", e)

    def play(self):
        try:
            self.mc.play()
        except Exception as e:
            log.warning("Resume failed: %s", e)

    def seek(self, position):
        try:
            self.mc.seek(position)
        except Exception as e:
            log.warning("Seek to %.1f failed: %s", position, e)

    def get_time(self):
        status = self.mc.status
        if status and status.current_time is not None:
            return status.current_time
        return 0.0

    def stop(self):
        try:
            self.mc.stop()
        except Exception as e:
            log.warning("Stop failed: %s", e)
        # Give the STOP command time to reach the Chromecast
        # before the connection is torn down.
        time.sleep(0.5)

    def disconnect(self):
        if self.cast:
            self.cast.disconnect()

    def quit_app(self):
        if self.cast:
            try:
                self.cast.quit_app(timeout=5)
            except Exception as e:
                log.warning("quit_app failed: %s", e)


class AudioController:
    def __init__(self, audio_path, delay_ms):
        self.audio_path = audio_path
        self.delay_ms = delay_ms
        self.instance = None
        self.player = None

    def start(self):
        self.instance = vlc.Instance(
            '--no-video', '--intf', 'dummy', '--quiet',
            '--file-logging',
            '--logfile=' + os.path.join(tempfile.gettempdir(), 'cast_vlc.log'),
        )
        self.player = self.instance.media_player_new()
        media = self.instance.media_new(self.audio_path)
        self.player.set_media(media)
        self.player.play()
        # audio_set_delay requires a running audio output
        time.sleep(0.2)
        self.player.audio_set_delay(self.delay_ms * 1000)

    def toggle_pause(self):
        self.player.set_pause(1 if self.player.is_playing() else 0)

    def pause(self):
        if self.player.is_playing():
            self.player.set_pause(1)

    def play(self):
        if not self.player.is_playing():
            self.player.set_pause(0)

    def seek(self, seconds):
        current = self.player.get_time()
        if current >= 0:
            new_time = max(0, current + int(seconds * 1000))
            self.player.set_time(new_time)

    def set_time(self, seconds):
        self.player.set_time(int(seconds * 1000))

    def get_time(self):
        t = self.player.get_time()
        return t / 1000.0 if t >= 0 else 0.0

    def get_length(self):
        l = self.player.get_length()
        return l / 1000.0 if l >= 0 else 0.0

    def is_playing(self):
        return self.player.is_playing()

    def stop(self):
        self.player.stop()

    def get_audio_track_count(self):
        try:
            return self.player.audio_get_track_count() or 0
        except Exception:
            return 0

    def get_audio_tracks_info(self):
        try:
            desc = self.player.audio_get_track_description()
            return list(desc) if desc else []
        except Exception:
            return []

    def get_current_audio_track_id(self):
        try:
            return self.player.audio_get_track()
        except Exception:
            return -1

    def get_current_audio_track_name(self):
        try:
            tid = self.player.audio_get_track()
            desc = self.player.audio_get_track_description()
            if desc:
                for item in desc:
                    if item[0] == tid:
                        return item[1]
        except Exception:
            pass
        return None

    def set_audio_track(self, track_id):
        if track_id < 0:
            return
        try:
            self.player.audio_set_track(track_id)
            time.sleep(0.1)
            self.player.audio_set_delay(self.delay_ms * 1000)
            log.info("Audio track switched to %d, delay %dms", track_id, self.delay_ms)
        except Exception as e:
            log.warning("Audio track switch failed: %s", e)


def run(stdscr, args):
    curses.use_default_colors()
    stdscr.nodelay(True)
    stdscr.clear()

    video_path = Path(args.video_path)
    if not video_path.exists():
        log.error("Video file not found: %s", video_path)
        _err(stdscr, f"File not found: {video_path}")
        return _pause_and_quit(stdscr, 1)

    # Probe video info and check Chromecast compatibility
    _msg(stdscr, 0, "Checking video compatibility...")
    info = probe_video(video_path)
    if info:
        log.info("Video: %s, %dx%d, container %s",
                 info['codec'], info['width'], info['height'], info['container'])
    warnings = check_compatibility(info)
    if warnings:
        for w in warnings:
            log.warning("Compatibility: %s", w)
            _msg(stdscr, 0, f"WARNING: {w[:curses.COLS - 10]}")
        time.sleep(3)

    # 1. Build Chromecast-compatible stream (remux or transcode)
    tmp_dir = tempfile.mkdtemp()
    cast_path = os.path.join(tmp_dir, 'video.mp4')

    try:
        build_cast_stream(str(video_path), cast_path, info, stdscr, 0, fast=args.fast, start_time=args.time)
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg failed:\n%s", e.stderr.decode(errors='replace') if e.stderr else str(e))
        _err(stdscr, "ffmpeg failed — see cast.log for details")
        _rm(tmp_dir)
        return _pause_and_quit(stdscr, 1)

    # 2. Extract subtitles with initial audio delay offset
    subtitle_tracks = extract_subtitles(str(video_path), tmp_dir, delay_ms=args.delay)
    if subtitle_tracks:
        log.info("Found %d subtitle track(s)", len(subtitle_tracks))

    # 3. Start HTTP server
    _msg(stdscr, 1, f"Starting HTTP server on port {HTTP_PORT}...")
    handler_cls = make_handler(tmp_dir)
    try:
        server = ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), handler_cls)
    except OSError:
        log.error("Port %d is already in use or blocked", HTTP_PORT)
        _err(stdscr, f"Port {HTTP_PORT} is already in use or blocked")
        _rm(tmp_dir)
        return _pause_and_quit(stdscr, 1)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # 4. Determine LAN IP
    if args.ip:
        local_ip = args.ip
        log.info("Using manual IP: %s", local_ip)
    else:
        local_ip = get_local_ip()
        if local_ip == '127.0.0.1':
            log.warning("Could not detect LAN IP — Chromecast may not reach the server")

    # Build subtitle URLs now that we have local_ip
    for t in subtitle_tracks:
        t['url'] = f"http://{local_ip}:{HTTP_PORT}/{t['url_path']}?v={int(time.time())}"

    # 5. Connect to Chromecast
    _msg(stdscr, 2, f"Discovering Chromecast '{args.name}'...")
    cast_ctrl = CastController(args.name)
    try:
        cast_ctrl.connect()
    except RuntimeError as e:
        log.error("Chromecast connection failed: %s", e)
        _err(stdscr, str(e))
        _rm(tmp_dir)
        return _pause_and_quit(stdscr, 1)

    # 6. Cast video
    video_url = f"http://{local_ip}:{HTTP_PORT}/video.mp4"
    mime = guess_mime(cast_path)
    log.info("Serving %s (Content-Type: %s) at %s", cast_path, mime, video_url)
    _msg(stdscr, 1, f"Serving: {video_url}")
    _msg(stdscr, 3, f"Casting to {args.name}...")
    try:
        cast_ctrl.cast_url(video_url, content_type=mime, current_time=args.time, subtitle_tracks=subtitle_tracks or None)
    except Exception as e:
        log.error("Failed to cast: %s", e)
        _err(stdscr, f"Failed to cast: {e}")
        _rm(tmp_dir)
        return _pause_and_quit(stdscr, 1)

    # 7. Start local audio
    _msg(stdscr, 4, f"Starting audio (delay: {args.delay}ms)...")
    audio_ctrl = AudioController(str(video_path), args.delay)    

    # Small wait for VLC to init
    if args.time == 0:
        time.sleep(0.75)
    else:
        time.sleep(0.25)
    audio_ctrl.start()

    if args.time > 0:
        audio_ctrl.set_time(args.time)
        _msg(stdscr, 4, f"Seeking audio to {format_time(args.time)}...")

    # Subtitles: auto-enabled at cast time — disable them so they start
    # in sync with audio instead of ahead of it.
    sub_idx = -1
    if subtitle_tracks:
        try:
            cast_ctrl.mc.disable_subtitle()
            log.info("Subtitles disabled until audio sync")
        except Exception as e:
            log.warning("Disable subtitle at startup failed: %s", e)
        time.sleep(0.3)
        sub_idx = 0
        try:
            cast_ctrl.mc.enable_subtitle(subtitle_tracks[0]['id'])
            t = subtitle_tracks[0]
            log.info("Subtitles enabled after audio sync: %s", t['name'])
        except Exception as e:
            log.warning("Enable subtitle after sync failed: %s", e)
    
    _msg(stdscr, 5, "Playing — press q to quit")
    stdscr.refresh()

    # 8. Interactive control loop
    paused = False
    running = True
    quitting = False
    audio_delay_ms = args.delay
    audio_track_ids = [t[0] for t in audio_ctrl.get_audio_tracks_info() if t[0] >= 0]
    audio_track_count = len(audio_track_ids)
    current_tid = audio_ctrl.get_current_audio_track_id()
    audio_track_idx = audio_track_ids.index(current_tid) if current_tid in audio_track_ids else 0

    def _shift_subtitles(offset_ms):
        for t in subtitle_tracks:
            shifted_path = os.path.join(tmp_dir, t['url_path'])
            shift_vtt_file(t['orig_path'], shifted_path, offset_ms)
            t['url'] = f"http://{local_ip}:{HTTP_PORT}/{t['url_path']}?v={int(time.time())}"

    while running:
        stdscr.erase()
        h, _ = stdscr.getmaxyx()

        cast_pos = cast_ctrl.get_time()
        audio_pos = audio_ctrl.get_time()
        audio_len = audio_ctrl.get_length()
        status = "PAUSED" if paused else "PLAYING"

        try:
            stdscr.addstr(0, 0, f" Chromecast  {format_time(cast_pos)}")
            stdscr.addstr(1, 0, f" Audio       {format_time(max(0, audio_pos))} / {format_time(audio_len)}")
            stdscr.addstr(2, 0, f" Status      {status}")
            stdscr.addstr(3, 0, f" Audio Delay  {audio_delay_ms}ms")

            info_y = 4
            if local_ip:
                stdscr.addstr(info_y, 0, f" IP          {local_ip}")
                info_y += 1
            if args.name != DEFAULT_NAME:
                stdscr.addstr(info_y, 0, f" Name        {args.name}")
                info_y += 1
            if args.time > 0:
                stdscr.addstr(info_y, 0, f" Start time  {format_time(args.time)}")
                info_y += 1
            if args.fast:
                stdscr.addstr(info_y, 0, f" Mode        fast remux")
                info_y += 1
            if args.software:
                stdscr.addstr(info_y, 0, f" Mode        software encode")
                info_y += 1
            if subtitle_tracks:
                if sub_idx == -1:
                    stdscr.addstr(info_y, 0, " Subtitles    Off")
                else:
                    t = subtitle_tracks[sub_idx]
                    stdscr.addstr(info_y, 0, f" Subtitles    {t['name']} ({sub_idx+1}/{len(subtitle_tracks)})")
                info_y += 1
            if audio_track_count > 1:
                at_name = audio_ctrl.get_current_audio_track_name() or f"Track {audio_track_idx+1}"
                stdscr.addstr(info_y, 0, f" Audio Track  {at_name} ({audio_track_idx+1}/{audio_track_count})")
                info_y += 1

            if quitting:
                stdscr.addstr(info_y, 0, " Quitting...")
                info_y += 1

            ctrl_lines = 4 + (1 if subtitle_tracks else 0) + (1 if audio_track_count > 1 else 0)
            help_y = max(info_y + 1, h - ctrl_lines)
            stdscr.addstr(help_y,     0, " ───────── Controls ─────────")
            stdscr.addstr(help_y + 1, 0, "  Space    pause / resume")
            help_off = 2
            if subtitle_tracks:
                stdscr.addstr(help_y + help_off, 0, "  s        cycle subtitles")
                help_off += 1
            if audio_track_count > 1:
                stdscr.addstr(help_y + help_off, 0, "  a        cycle audio tracks")
                help_off += 1
            stdscr.addstr(help_y + help_off, 0, "  [ / ]    delay -/+100ms")
            help_off += 1
            stdscr.addstr(help_y + help_off, 0, "  q        quit")

        except:
            pass

        stdscr.refresh()
        key = stdscr.getch()

        if key == ord(' '):
            paused = not paused
            if paused:
                log.info("Pause")
                cast_ctrl.pause()
                audio_ctrl.toggle_pause()
            else:
                log.info("Resume")
                cast_ctrl.play()
                audio_ctrl.toggle_pause()
        elif key in (ord('s'), ord('S')):
            if subtitle_tracks:
                sub_idx += 1
                if sub_idx >= len(subtitle_tracks):
                    sub_idx = -1
                if sub_idx == -1:
                    try:
                        cast_ctrl.mc.disable_subtitle()
                    except Exception as e:
                        log.warning("Disable subtitle failed: %s", e)
                    log.info("Subtitles off")
                else:
                    track = subtitle_tracks[sub_idx]
                    try:
                        cast_ctrl.mc.enable_subtitle(track['id'])
                    except Exception as e:
                        log.warning("Enable subtitle %d failed: %s", track['id'], e)
                    log.info("Subtitles: %s (%d/%d)", track['name'], sub_idx + 1, len(subtitle_tracks))
        elif key in (ord('a'), ord('A')):
            if audio_track_count > 1:
                audio_track_idx = (audio_track_idx + 1) % audio_track_count
                audio_ctrl.set_audio_track(audio_track_ids[audio_track_idx])
        elif key == ord('['):
            audio_delay_ms = max(0, audio_delay_ms - 100)
            audio_ctrl.player.audio_set_delay(audio_delay_ms * 1000)
            log.info("Delay: %dms", audio_delay_ms)
            if subtitle_tracks:
                _shift_subtitles(audio_delay_ms)
                if sub_idx >= 0:
                    cast_ctrl.update_subtitle_track(subtitle_tracks, sub_idx)
        elif key == ord(']'):
            audio_delay_ms = min(10000, audio_delay_ms + 100)
            audio_ctrl.player.audio_set_delay(audio_delay_ms * 1000)
            log.info("Delay: %dms", audio_delay_ms)
            if subtitle_tracks:
                _shift_subtitles(audio_delay_ms)
                if sub_idx >= 0:
                    cast_ctrl.update_subtitle_track(subtitle_tracks, sub_idx)
        # TODO: Try to think in another way make it work, now every time it pressed it start the video from the begining.
        # elif key == curses.KEY_LEFT:
        #     log.info("Seek -5s")
        #     audio_ctrl.pause()
        #     time.sleep(0.2)
        #     pos = max(0, audio_ctrl.get_time() + (audio_delay_ms / 1000.0) - 5)
        #     audio_ctrl.seek(-5)
        #     cast_ctrl.reload_at(video_url, mime, pos, subtitle_tracks=subtitle_tracks or None)
        #     if not paused:
        #         audio_ctrl.play()
        # elif key == curses.KEY_RIGHT:
        #     log.info("Seek +5s")
        #     audio_ctrl.pause()
        #     time.sleep(0.2)
        #     pos = audio_ctrl.get_time() + (audio_delay_ms / 1000.0) + 5
        #     audio_ctrl.seek(5)
        #     cast_ctrl.reload_at(video_url, mime, pos, subtitle_tracks=subtitle_tracks or None)
        #     if not paused:
        #         audio_ctrl.play()
        elif key == ord('q') and not quitting:
            log.info("Quit requested")
            quitting = True

        if quitting:
            running = False
        elif not paused and not audio_ctrl.is_playing():
            time.sleep(1)
            if not audio_ctrl.is_playing():
                running = False

        time.sleep(0.1)

    return _cleanup(tmp_dir, server, cast_ctrl, audio_ctrl, 0)


def _msg(stdscr, y, text):
    try:
        stdscr.addstr(y, 0, text[:curses.COLS - 1])
        stdscr.clrtoeol()
        stdscr.refresh()
    except curses.error:
        pass


def _err(stdscr, text):
    try:
        stdscr.addstr(0, 0, f"ERROR: {text[:curses.COLS - 8]}", curses.A_BOLD)
        stdscr.clrtoeol()
        stdscr.refresh()
    except curses.error:
        pass


def _rm(path):
    if path and os.path.exists(path):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError:
            pass


def _pause_and_quit(stdscr, code):
    stdscr.nodelay(False)
    try:
        stdscr.addstr(0, 0, "Press any key to exit")
        stdscr.clrtoeol()
        stdscr.refresh()
    except curses.error:
        pass
    stdscr.getch()
    return code


def _cleanup(tmp_dir, server, cast_ctrl, audio_ctrl, code):
    try:
        if audio_ctrl:
            audio_ctrl.stop()
        if cast_ctrl:
            cast_ctrl.quit_app()
            cast_ctrl.stop()
            cast_ctrl.disconnect()
        if server:
            server.shutdown()
        if tmp_dir and os.path.exists(tmp_dir):
            try:
                shutil.rmtree(tmp_dir)
            except OSError as e:
                log.warning("Temp dir cleanup failed: %s", e)
    except Exception as e:
        log.error("Cleanup error: %s", e)
    return code


def main():
    parser = argparse.ArgumentParser(
        description="Cast video to Chromecast (video-only), keep audio on the local machine"
    )
    parser.add_argument('video_path', help="Path to the video file")
    parser.add_argument('-n', '--name', default=DEFAULT_NAME,
                        help=f"Chromecast device name (default: {DEFAULT_NAME})")
    parser.add_argument('-d', '--delay', type=int, default=DEFAULT_DELAY,
                        help=f"Audio delay in ms (default: {DEFAULT_DELAY})")
    parser.add_argument('--fast', action='store_true',
                        help="Skip transcoding (instantly remux to MP4 — video may not play on Chromecast)")
    parser.add_argument('--software', action='store_true',
                        help="Force software encoding (slower but may fix playback issues)")
    parser.add_argument('--ip', type=str,
                        help="LAN IP address for the HTTP server (auto-detected if not set)")

    def _parse_time(val):
        """Accept seconds (300) or HH:MM:SS (00:05:00)."""
        if ':' in val:
            parts = val.split(':')
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
        return float(val)

    parser.add_argument('-t', '--time', type=_parse_time, default=0, metavar='SEC|HH:MM:SS',
                        help="Start position (default: 0). Examples: -t 300  -t 00:05:00  -t 01:20:15")
    args = parser.parse_args()

    if args.software:
        force_software_encoder()

    try:
        rc = curses.wrapper(run, args)
        if rc != 0:
            print(f"\nError — details logged to {_LOG_PATH}", file=sys.stderr)
        sys.exit(rc)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == '__main__':
    main()
