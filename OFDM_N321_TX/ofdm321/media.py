"""FFmpeg command lines (encoder options taken from OFDM_Video_N210 v1.23 media.py)."""
import hashlib
import shutil
from pathlib import Path


def require_ffmpeg():
    for exe in ("ffmpeg", "ffprobe"):
        if shutil.which(exe) is None:
            raise RuntimeError(f"{exe} not found; run scripts/setup_ubuntu.sh")


def encoder_options(s):
    low_rate = s.ts_muxrate <= 200_000
    return [
        "-an", "-vf", f"scale={s.width}:{s.height}:force_original_aspect_ratio=decrease,"
        f"pad={s.width}:{s.height}:(ow-iw)/2:(oh-ih)/2,fps={s.fps}",
        "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-b:v", str(s.video_bitrate), "-maxrate", str(s.video_bitrate),
        "-bufsize", str(s.video_bitrate), "-g", str(s.fps), "-keyint_min", str(s.fps),
        "-sc_threshold", "0", "-x264-params", "repeat-headers=1:scenecut=0",
        "-f", "mpegts", "-muxrate", str(s.ts_muxrate), "-mpegts_flags", "+resend_headers",
        "-muxdelay", "0.7" if low_rate else "0.1", "-pat_period", "0.5" if low_rate else "0.2",
        "-sdt_period", "1.0", "-flush_packets", "1",
    ]


def encoder_command(s, source, input_path, seconds):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
    if source == "testpattern":
        cmd += ["-re", "-f", "lavfi", "-i", f"testsrc2=size={s.width}x{s.height}:rate={s.fps}"]
    elif source == "webcam":
        cmd += ["-f", "v4l2", "-framerate", str(s.fps), "-video_size", f"{s.width}x{s.height}",
                "-i", input_path or "/dev/video0"]
    elif source == "file":
        path = Path(input_path or "").expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Video input not found: {path}")
        cmd += ["-re", "-i", str(path)]
    else:
        raise ValueError("source must be testpattern, webcam or file")
    if seconds > 0:
        cmd += ["-t", str(seconds)]
    return cmd + encoder_options(s) + ["pipe:1"]


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
