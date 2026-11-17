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
MODULATIONS = {"qpsk": 2, "16qam": 4}
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
