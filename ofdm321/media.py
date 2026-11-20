"""FFmpeg command lines (encoder options taken from OFDM_Video_N210 v1.23 media.py)."""
import hashlib
import shutil
from pathlib import Path


def require_ffmpeg():
    for exe in ("ffmpeg", "ffprobe"):
        if shutil.which(exe) is None:
            raise RuntimeError(f"{exe} not found; run scripts/setup_ubuntu.sh")


def video_scale(s):
    return (f"scale={s.width}:{s.height}:force_original_aspect_ratio=decrease,"
            f"pad={s.width}:{s.height}:(ow-iw)/2:(oh-ih)/2,fps={s.fps}")


def codec_mux_options(s, video_bitrate, ts_muxrate, muxdelay=None):
    """libx264 + CBR MPEG-TS options. GOP = fps frames, no scene cut: keyframes at fixed
    positions (needed to splice renditions in video_adapt)."""
    low_rate = ts_muxrate <= 200_000
    if muxdelay is None:
        muxdelay = "0.7" if low_rate else "0.1"
    return [
        "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-b:v", str(video_bitrate), "-maxrate", str(video_bitrate),
        "-bufsize", str(video_bitrate), "-g", str(s.fps), "-keyint_min", str(s.fps),
        "-sc_threshold", "0", "-x264-params", "repeat-headers=1:scenecut=0",
        "-f", "mpegts", "-muxrate", str(ts_muxrate), "-mpegts_flags", "+resend_headers",
        "-muxdelay", muxdelay, "-pat_period", "0.5" if low_rate else "0.2",
        "-sdt_period", "1.0", "-flush_packets", "1",
    ]


def encoder_options(s):
    return ["-an", "-vf", video_scale(s)] + codec_mux_options(s, s.video_bitrate, s.ts_muxrate)


def input_options(s, source, input_path):
    if source == "testpattern":
        return ["-re", "-f", "lavfi", "-i", f"testsrc2=size={s.width}x{s.height}:rate={s.fps}"]
    if source == "webcam":
        return ["-f", "v4l2", "-framerate", str(s.fps), "-video_size", f"{s.width}x{s.height}",
                "-i", input_path or "/dev/video0"]
    if source == "file":
        path = Path(input_path or "").expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Video input not found: {path}")
        return ["-re", "-i", str(path)]
    raise ValueError("source must be testpattern, webcam or file")


def encoder_command(s, source, input_path, seconds):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"] + input_options(s, source, input_path)
    if seconds > 0:
        cmd += ["-t", str(seconds)]
    return cmd + encoder_options(s) + ["pipe:1"]


def ladder_command(s, source, input_path, seconds, rates, fds):
    """One FFmpeg, one decoded source, len(rates) TS renditions [(ts_muxrate, video_bitrate)],
    rendition i written to file descriptor fds[i]. Same muxdelay everywhere -> same PTS."""
    inp = input_options(s, source, input_path)
    if seconds > 0:                              # input option: limits every output alike
        inp = inp[:-2] + ["-t", str(seconds)] + inp[-2:]
    n = len(rates)
    muxdelay = "0.7" if min(m for m, _ in rates) <= 200_000 else "0.1"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"] + inp + [
        "-filter_complex", f"[0:v]{video_scale(s)},split={n}" + "".join(f"[v{i}]" for i in range(n))]
    for i, ((mux, vb), fd) in enumerate(zip(rates, fds)):
        cmd += ["-map", f"[v{i}]", "-an"] + codec_mux_options(s, vb, mux, muxdelay) + [f"pipe:{fd}"]
    return cmd


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
