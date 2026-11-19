"""Settings for the N321 2x2 MIMO OFDM video link (one N321, loopback)."""
from dataclasses import dataclass, asdict, fields
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Application framing reused byte-for-byte from OFDM_Video_N210 v1.23.
TS_CHUNK_BYTES = 188
APP_HEADER_BYTES = 24
PACKET_BYTES = APP_HEADER_BYTES + TS_CHUNK_BYTES          # 212

MIMO_MODES = ("siso", "simo", "alamouti", "smux")
MODULATIONS = {"bpsk": 1, "qpsk": 2, "16qam": 4, "64qam": 6}
MASTER_CLOCK_RATES = (200e6, 245.76e6, 250e6)


@dataclass
class Settings:
    # --- PHY -------------------------------------------------------------
    mimo_mode: str = "alamouti"      # siso | simo | alamouti | smux
    modulation: str = "qpsk"         # qpsk | 16qam
    sample_rate: float = 1_000_000
    frame_gap_samples: int = 80
    sync_threshold: float = 0.60
    tx_rms: float = 0.10             # per TX channel, digital fc32 full scale = 1.0
    interleaver: str = "block"       # TX: none | block | carrier (RX follows the SIG field)
    soft_decoding: bool = True       # RX: reliability-ranked erasure decoding of RS(248,216)
    const_every: int = 8             # RX: one constellation snapshot every N good frames (0 = off)
    # --- adaptive coding & modulation (both sides must agree on acm) ----
    acm: bool = False                # per-frame modulation chosen from RX feedback
    acm_modcods: str = "bpsk,qpsk,16qam,64qam"   # TX: allowed modulations (ladder)
    acm_margin_db: float = 1.0       # TX: extra SNR above the calibrated threshold
    acm_host: str = "127.0.0.1"      # RX: IP of the TX PC (feedback destination)
    acm_port: int = 5003             # UDP port of the feedback channel
    acm_fb_delay_s: float = 0.0      # RX: extra feedback delay (0.5 ~ GEO satellite round trip)
    fb_link: str = "udp"             # ACM feedback path: udp (network) | radio (N321 channel B, SISO only)
    fb_freq: float = 922.7e6         # radio feedback carrier (RX machine TX/RX B -> TX machine RX2 B)
    fb_rate: float = 500e3           # radio feedback sample rate (MCR / even integer); CFO range +-fb_rate/64
    fb_tx_gain: float = 30.0         # RX machine, TX/RX B = RF1 (antenna: <= 30)
    fb_rx_gain: float = 60.0         # TX machine, RX2 B = RF1
    fb_zmq_addr: str = "tcp://127.0.0.1:5561"   # simulation: feedback link (RX PUSH -> TX PULL)
    sim_fb_snr_db: float = 20.0      # simulation: SNR of the feedback link
    # --- N321 ------------------------------------------------------------
    device_args: str = "addr=192.168.20.2,master_clock_rate=200e6"
    subdev: str = "A:0 B:0"
    frequency: float = 915e6
    tx_gain: float = 10.0
    rx_gain: float = 30.0
    tx_antenna: str = "TX/RX"
    rx_antenna: str = "RX2"
    clock_source: str = "internal"
    time_source: str = "internal"
    start_delay_s: float = 1.0
    # --- video -----------------------------------------------------------
    width: int = 640
    height: int = 360
    fps: int = 15
    video_bitrate: int = 200_000
    ts_muxrate: int = 260_000
    fec_mode: str = "rs8_8"
    queue_packets: int = 64
    udp_port: int = 5002
    # --- simulation channel ---------------------------------------------
    sim_snr_db: float = 25.0
    sim_cfo_hz: float = 0.0
    sim_channel: str = "rayleigh"    # identity | rayleigh | fixed
    sim_seed: int = 1
    sim_sfo_ppm: float = 0.0          # two-machine simulation: sample-clock offset
    zmq_addr: str = "tcp://127.0.0.1:5560"   # two-process simulation link (TX PUSH -> RX PULL)
    sim_snr_profile: str = ""         # time-varying SNR: sine:6:30:40 | steps:28,18,8:10 | ramp:30:5:60

    @property
    def bits_per_symbol(self):
        return MODULATIONS[self.modulation]

    @property
    def n_tx(self):
        return 2 if self.mimo_mode in ("alamouti", "smux") else 1

    @property
    def n_rx_used(self):
        return 1 if self.mimo_mode == "siso" else 2

    @property
    def master_clock_rate(self):
        for item in self.device_args.split(","):
            if item.strip().startswith("master_clock_rate="):
                return float(item.split("=", 1)[1])
        return 200e6

    def validate(self):
        if self.mimo_mode not in MIMO_MODES:
            raise ValueError(f"mimo_mode must be one of {MIMO_MODES}")
        if self.interleaver not in ("none", "block", "carrier"):
            raise ValueError("interleaver must be none, block or carrier")
        if self.modulation not in MODULATIONS:
            raise ValueError(f"modulation must be one of {tuple(MODULATIONS)}")
        mcr = self.master_clock_rate
        if not any(math.isclose(mcr, m) for m in MASTER_CLOCK_RATES):
            raise ValueError(f"N321 master_clock_rate must be one of {MASTER_CLOCK_RATES}")
        decim = mcr / self.sample_rate
        if abs(decim - round(decim)) > 1e-9 or not 1 <= round(decim) <= 1024:
            raise ValueError(f"sample_rate must be MCR/N with integer N in 1..1024 (MCR={mcr:g})")
        if round(decim) % 2:
            raise ValueError("Use an even decimation (UHD N320/N321 recommendation)")
        if not 3e6 <= self.frequency <= 6e9:
            raise ValueError("N321 tuning range is 3 MHz .. 6 GHz")
        if not 0 <= self.tx_gain <= 60 or not 0 <= self.rx_gain <= 60:
            raise ValueError("N321 gains are 0..60 dB")
        if not 0 < self.tx_rms <= 0.3:
            raise ValueError("tx_rms must be in (0, 0.3] to keep OFDM peaks below DAC full scale")
        if self.fec_mode != "rs8_8":
            raise ValueError("Only verified outer RS8+8 is supported")
        if not 0.3 <= self.sync_threshold <= 0.95:
            raise ValueError("sync_threshold must be in [0.3, 0.95]")
        if self.clock_source not in ("internal", "external", "gpsdo") or \
                self.time_source not in ("internal", "external", "gpsdo"):
            raise ValueError("clock/time source must be internal, external or gpsdo")
        if self.subdev.split() != ["A:0", "B:0"]:
            raise ValueError("N321 two-channel subdev must be 'A:0 B:0'")
        if self.fb_link not in ("udp", "radio"):
            raise ValueError("fb_link must be udp or radio")
        if self.fb_link == "radio":
            if not self.acm:
                raise ValueError("fb_link radio is the ACM feedback path: add --acm")
            if self.mimo_mode != "siso":
                raise ValueError("fb_link radio uses channel B of both N321: only --mimo siso")
            d = mcr / self.fb_rate
            if abs(d - round(d)) > 1e-9 or round(d) % 2 or not 2 <= round(d) <= 1024:
                raise ValueError(f"fb_rate must be MCR/N with an even N in 2..1024 (MCR={mcr:g})")
            if not 3e6 <= self.fb_freq <= 6e9:
                raise ValueError("fb_freq out of the N321 range")
            if abs(self.fb_freq - self.frequency) < 0.5 * (self.sample_rate + self.fb_rate):
                raise ValueError("fb_freq overlaps the video channel: keep them >= 1 MHz apart")
            if not 0 <= self.fb_tx_gain <= 60 or not 0 <= self.fb_rx_gain <= 60:
                raise ValueError("feedback gains are 0..60 dB")
        if self.acm:
            from .acm import parse_modcods
            parse_modcods(self.acm_modcods)
            if not 1024 <= self.acm_port <= 65535 or self.acm_port == self.udp_port:
                raise ValueError("acm_port must be 1024..65535 and differ from udp_port")
        return self

    def to_dict(self):
        return asdict(self)


def load_settings(path=None, **overrides):
    data = {}
    if path:
        data = json.loads(Path(path).read_text())
    known = {f.name for f in fields(Settings)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown config keys: {sorted(unknown)}")
    data.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**data).validate()
