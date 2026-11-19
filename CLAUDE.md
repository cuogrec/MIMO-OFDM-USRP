# CLAUDE.md — MIMO-OFDM-USRP

Real-time video over an OFDM link (2x2 MIMO or SISO) between two USRP N321, written in
Python + numpy on GNU Radio 3.10.9 / UHD 4.6 (Ubuntu 24.04). Current version: v0.4.0.

**Reply to the user in Vietnamese.** Short, concrete answers; give exact commands to run.

## Lab setup (fixed facts)

| | PC | Interface | N321 | Serial | Role | Antennas |
|---|---|---|---|---|---|---|
| RX | `thuy-anh` (HP 280 Pro G8) | `enp1s0` 192.168.180.1/24 | 192.168.180.95, MTU 1500 | 32550F7 | receiver | RX2 A (video); TX/RX B (radio feedback, v0.4) |
| TX | `cuong` (Legion 5 Pro) | `enp93s0` 192.168.180.1/24 | 192.168.180.3, MTU 9000 | 3258433 | transmitter, loopback | TX/RX A (video); RX2 B (radio feedback, v0.4) |

- Both N321: FPGA 8.2 HG image (SFP0 = 1 GbE, RJ45 SFP module), MPM 5.3, has GPSDO. UHD product name `n320`.
- MCR 200 MHz, video 1 MS/s (decim 200), subdev `A:0 B:0`, sc16 over the wire.
- Two separate PCs, each with ONE Ethernet port wired to its N321. The lab Wi-Fi isolates clients:
  the two PCs cannot reach each other over the network (hence radio feedback).
- On the RX PC, `OFDM_N321_RX/config/default.json` was edited locally to `addr=192.168.180.95`
  (the committed file still says `.4`). Don't overwrite that local edit; pass `--args` if in doubt.
- Run project Python with `/usr/bin/python3` (scripts already do).

## RF safety — always

- Antenna link: TX gain <= 30 dB (`validate_rf`, `validate_tx_gain` enforce it). Cable link: attenuator
  >= 30 dB and `--attenuation-db`.
- Transmit only in 920–923 MHz (Vietnam). Video default in use: `--freq 921e6`; radio feedback 922.7 MHz.
- Ask the user before starting any command that transmits RF (`start_tx.sh`, `run_antenna_915.sh`,
  `start_rx.sh --fb-link radio` also transmits on channel B).

## Repo layout

Three self-contained folders, each with its OWN copy of the `ofdm321` package. When you change
`ofdm321/*.py`, `tests/`, `tools/` or shared docs, change `OFDM_N321_RX` and copy the same files to
`OFDM_N321_TX` and `OFDM_N321_MIMO` (they must stay identical; check with `diff -rq`).

| Folder | Use | Entry points |
|---|---|---|
| `OFDM_N321_TX` | N321 #1 transmitter | `scripts/start_tx.sh`, `scripts/sim_tx*.sh` → `run_tx.py` → `ofdm321/runner_split.py:main_tx` |
| `OFDM_N321_RX` | N321 #2 receiver (run FIRST) | `scripts/start_rx.sh`, `scripts/sim_rx*.sh` → `run_rx.py` → `main_rx` |
| `OFDM_N321_MIMO` | loopback on one N321, 4 antennas | `scripts/run_antenna_915.sh`, `scripts/run_sim.sh` → `run_n321.py` → `ofdm321/runner.py` |
| `grc/`, `*/grc/` | GNU Radio Companion views (Embedded Python blocks call `ofdm321`) | fixed modulation only, no ACM |

`ofdm321/` modules:
- `modem.py` — OFDM frame (FFT 64, CP 16, 48 data + 4 pilots; STF w/ CSD, 2 P-matrix LTFs, SIG CRC16),
  Modulator/Demodulator (Schmidl-Cox w/ Cauchy-Schwarz metric, CFO, H 2x2, CPE, MRC/SFBC/MMSE,
  batched numpy), interleavers none/block/carrier, soft errors-and-erasures RS, ACM frames
  (fixed 42 data symbols, 1/2/4/6 codewords for BPSK/QPSK/16QAM/64QAM, repeated 24-bit SIG + CRC8).
- `inner_rs.py`, `soft_inner_rs.py`, `fec_rs.py`, `verified_fec.py`, `protocol.py` — RS(248,216) inner code,
  212-byte `OVN1` app packets, verified RS8+8 outer code (needs 10/16 shards). Wire format shared with the
  older N210 project: do not change it.
- `blocks.py` — GNU Radio blocks: `MimoTxSource`, `MimoRxSink` (ring buffer 2^18, acquire/track, ports
  `pdus` and `const`), `MimoChannelSim` (+ `--snr-profile`), `SfoResampler` (16-tap windowed sinc),
  `VideoReceiver`, `FeedbackRadioSource`.
- `acm.py` — `AcmController` (thresholds 9/14/20/26 dB SNR_eff, margin 1, hysteresis 1, hold 0.5 s,
  confirmation-based error handling, 1.5 s timeout), `FeedbackReporter`, UDP `FeedbackSender`/`FeedbackReceiver`,
  radio feedback packet `OFB1` + `RadioFeedbackHandler`.
- `hardware.py` — UHD setup, timed start, LO-lock checks, per-channel streams for radio feedback.
- `config.py` — `Settings` dataclass + `config/default.json`; every CLI option maps to a field.
- `rxgui.py` — `--gui`: constellation + 2-channel spectrum + status line.
- `runner_split.py` — TX/RX programs, write `runs/<stamp>_<tx|rx>_<rf|sim>_<mimo>_<mod>/report.json`.

## Commands

```bash
python3 OFDM_N321_RX/tests/test_modem.py          # 11 tests, must all PASS before committing
python3 OFDM_N321_TX/tools/probe_n321.py --addr 192.168.180.3 --stream   # device + network check, no RF

# two-process simulation on one PC (RX first, then TX in another terminal)
bash OFDM_N321_RX/scripts/sim_rx.sh --play            ; bash OFDM_N321_TX/scripts/sim_tx.sh
bash OFDM_N321_RX/scripts/sim_rx_acm.sh --gui         ; bash OFDM_N321_TX/scripts/sim_tx_acm.sh
bash OFDM_N321_RX/scripts/sim_rx_fbradio.sh --gui     ; bash OFDM_N321_TX/scripts/sim_tx_fbradio.sh

# RF, two PCs (RX PC first)
bash scripts/start_rx.sh --play --gui --freq 921e6 --rx-gain 50                       # in OFDM_N321_RX
bash scripts/start_tx.sh --freq 921e6 --tx-gain 30 --source file --input <video> --seconds 60   # in OFDM_N321_TX
# ACM with radio feedback (v0.4, not yet tried on RF): add to BOTH sides
#   --mimo siso --acm --fb-link radio   (+ RX: --fb-tx-gain 20, TX: --fb-rx-gain 40 --acm-modcods qpsk,16qam,64qam)
```

Both sides must use the same `--mimo`, `--mod`, `--rate`, `--freq` (and `--fb-freq`, `--fb-rate`).
Success = RX prints `RF_RX_VERIFIED` / `SIM_RX_VERIFIED` (video SHA-256 identical).

Reading the RX log: `ok` ≈ 481 frames/s (Alamouti/SISO QPSK); `sig/rs/crc` small; `lost` ~0;
QPSK needs EVM < −12 dB, 16QAM < −20 dB; `cond` < 10–15 dB for smux; `rx_rms` 0.005–0.2.
`same_host_queue_to_rx_latency` is meaningless across two PCs.

## Status (2026-11-19)

- RF loopback (one N321): PASS. RF two N321, Alamouti QPSK, 921 MHz, gain 30/50: success, SNR 26 dB,
  EVM −22 dB, 0 errors while observed. RX B branch ~10 dB weaker than RX A (cond ~31 dB): unresolved.
- ACM: works in simulation (+71% mean capacity vs fixed QPSK, 1.3 % loss). Not run on RF yet.
- Radio feedback (v0.4): works in simulation (loop latency 0.04–0.09 s). Not run on RF yet.
- Video bitrate is fixed (260 kb/s TS) and does not follow ACM.

## Next steps (agreed direction)

1. First RF run of `--mimo siso --acm --fb-link radio`; tune fb gains, watch self-interference.
2. Fix/diagnose the weak RX B branch (swap antennas).
3. Systematic measurements script (loss/EVM vs gain and mode, ACM vs fixed), plots for the report.
4. ACM-driven video bitrate; LDPC instead of RS; GPSDO sync; later a DVB-S2-style single-carrier modem.

Docs: `OFDM_N321_*/docs/` — `THIET_KE.md`, `HUONG_DAN_2_N321.md`, `GIAI_MEM_INTERLEAVING.md`, `ACM.md`,
`PHAN_HOI_RADIO.md`, `MOT_CONG_MANG.md`. Full project report (Vietnamese) is a Claude Doc:
https://claude.ai/artifact/FYd3drF6E8f9nVxDoSjdiX

## Conventions

- Keep the real-time budget: no per-sample Python loops in `work()`; batch with numpy.
- New feature → simulate first (ZeroMQ two-process), add a test in `tests/test_modem.py`, then RF.
- Commit messages in English; never commit `runs/`, `__pycache__`, video files.
