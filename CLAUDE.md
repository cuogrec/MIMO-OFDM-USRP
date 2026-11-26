# CLAUDE.md — MIMO-OFDM-USRP

Real-time video over an OFDM link (2x2 MIMO or SISO) between two USRP N321, written in
Python + numpy on GNU Radio 3.10.9 / UHD 4.6 (Ubuntu 24.04). Current version: v0.6.2.

**Reply to the user in Vietnamese.** Short, concrete answers; give exact commands to run.

## Lab setup (fixed facts)

| | PC | Interface | N321 | Serial | Role | Antennas |
|---|---|---|---|---|---|---|
| RX | `thuy-anh` (HP 280 Pro G8) | `enp1s0` 192.168.180.1/24 | 192.168.180.95, MTU 1500 | 32550F7 | receiver | RX2 A/RF0 (video); TX/RX B/RF1 (radio feedback, v0.4) |
| TX | `cuong` (Legion 5 Pro) | `enp93s0` 192.168.180.1/24 | 192.168.180.3, MTU 9000 | 3258433 | transmitter, loopback | TX/RX A/RF0 (video); RX2 B/RF1 (radio feedback, v0.4) |

- Both N321: FPGA 8.2 HG image (SFP0 = 1 GbE, RJ45 SFP module), MPM 5.3, has GPSDO. UHD product name `n320`.
- Channel A = **RF0**, channel B = **RF1** (front-panel labels on the N321); UHD subdev `A:0` = ch 0, `B:0` = ch 1.
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
  `start_rx.sh --fb-link radio` also transmits on channel B = RF1).

## Repo layout

ONE shared package `ofdm321/` at the repo root (`pyproject.toml`, `pip install -e .`, done by
`scripts/setup_ubuntu.sh` with `--user --break-system-packages --no-build-isolation`). `tests/` and `tools/` are at
the root too. The three machine folders keep only `scripts/`, `config/`, `docs/`, `grc/`, `run_*.py` (+ `runs/`);
`OFDM_N321_*/tools` and `OFDM_N321_*/tests` are symlinks to the root, so old commands still work.

- `run_*.py` add the repo root to `sys.path` when the package is not installed, then `set_app_root(<folder>)`.
  `ofdm321.config.app_root()` = the running folder: `config/default.json` and `runs/` are per folder.
  Tools use `app_root()` = the folder you run them from (`cd OFDM_N321_TX && python3 tools/probe_n321.py`).
- GRC Embedded blocks: `_project_root()` imports an installed `ofdm321`, else finds `<repo>/ofdm321` from the
  flowgraph location, `$OFDM_N321_DIR`, `~/MIMO-OFDM-USRP`. GRC files are written with GRC's own YAML dumper.
- Per-folder docs: `OFDM_N321_RX/docs` and `OFDM_N321_TX/docs` are identical copies (keep them so).

| Folder | Use | Entry points |
|---|---|---|
| `OFDM_N321_TX` | N321 #1 transmitter | `scripts/start_tx.sh`, `scripts/sim_tx*.sh` → `run_tx.py` → `ofdm321/runner_split.py:main_tx` |
| `OFDM_N321_RX` | N321 #2 receiver (run FIRST) | `scripts/start_rx.sh`, `scripts/sim_rx*.sh` → `run_rx.py` → `main_rx` |
| `OFDM_N321_MIMO` | loopback on one N321, 4 antennas | `scripts/run_antenna_915.sh`, `scripts/run_sim.sh` → `run_n321.py` → `ofdm321/runner.py` |
| `grc/`, `*/grc/` | GNU Radio Companion views (Embedded Python blocks call `ofdm321`) | fixed modulation only, no ACM |

`ofdm321/` modules (repo root):
- `modem.py` — OFDM frame (FFT 64, CP 16, 48 data + 4 pilots; STF w/ CSD, 2 P-matrix LTFs, SIG CRC16),
  Modulator/Demodulator (Schmidl-Cox w/ Cauchy-Schwarz metric on the used antennas only, threshold 0.35,
  v0.6 LTF fine timing + SIG-less recovery (`--legacy-sync` = v0.5 front end), CFO, H 2x2, CPE, MRC/SFBC/MMSE,
  batched numpy), interleavers none/block/carrier, soft errors-and-erasures RS, ACM frames
  (fixed 42 data symbols, 1/2/4/6 codewords for BPSK/QPSK/16QAM/64QAM, repeated 24-bit SIG + CRC8).
- `ldpc.py` (v0.6) — 802.11n QC-LDPC (n 648/1296/1944, rates 1/2..5/6, base matrices of the standard), encoder
  via the dual-diagonal structure, layered normalised min-sum (numpy, batched, early stop), `PacketCoder`: one
  212-byte packet + CRC32 = 1728 bits over 2 shortened codewords (ACM frames: 5/6 punctured to 2016 bits).
  `--fec rs|ldpc` (+ `--ldpc-rate`, fixed frames) on BOTH sides; SIG byte 1 bit 6 = LDPC (fixed SIG: rate in the
  top 2 seq bits); a receiver with the other code counts `fec_mismatch`. Max-log LLRs (`modem.bit_llr`) with the
  post-EQ SNR weights, random bit interleaver (`modem.bit_perm`). ACM thresholds per code (`acm.THRESHOLDS_BY_FEC`).
- `inner_rs.py`, `soft_inner_rs.py`, `fec_rs.py`, `verified_fec.py`, `protocol.py` — RS(248,216) inner code,
  212-byte `OVN1` app packets, verified RS8+8 outer code (needs 10/16 shards). Wire format shared with the
  older N210 project: do not change it.
- `blocks.py` — GNU Radio blocks: `MimoTxSource` (ACM: resend on downshift, `--acm-resend`), `MimoRxSink`
  (ring buffer 2^18, acquire/track, ports `pdus` and `const`), `MimoChannelSim` (+ `--snr-profile`),
  `SfoResampler` (16-tap windowed sinc), `VideoReceiver` (0.3 s reorder buffer with ACM, `--reorder`),
  `FeedbackRadioSource`.
- `acm.py` — `AcmController` (thresholds 9/14/20/26 dB SNR_eff, margin 1, hysteresis 1, hold 0.5 s,
  confirmation-based error handling, 1.5 s timeout, `mod_since`; v0.6.1 debounce: SNR step-down after 3 fresh
  reports over 30 ms, not within 0.3 s of a confirmed step up; CER > 10 % or SNR below the lower threshold: at
  once; `--no-acm-debounce` = v0.6.0), `FeedbackReporter`, UDP `FeedbackSender`
  (every 0.02 s)/`FeedbackReceiver`, radio feedback packet `OFB1` + `RadioFeedbackHandler`, `FeedbackWatchdog`.
- `video_adapt.py` (v0.5) — ACM-driven video bitrate: one FFmpeg, one TS rendition per modulation (75 % of its
  capacity), spliced at keyframes (GOP 1 s) with TS continuity counters rewritten; down at once, up after 5 s stable.
- `hardware.py` — UHD setup, timed start, LO-lock checks, per-channel streams for radio feedback,
  `port_label()` (RF0/RF1 names) and `antenna_summary()` (start-up line "Cắm anten ...").
- `config.py` — `Settings` dataclass + `config/default.json`; every CLI option maps to a field.
- `rxgui.py` — `--gui`: constellation + 2-channel spectrum + status line.
- TX latency (v0.6.1): `blocks.AirClock` maps sample index -> air time (USRP timed start + get_time_now()),
  `TxPacing` keeps both TX sources <= `--tx-lead-ms` (50 since v0.6.2) ahead of the air; `TxDeviceSim` + a GNU Radio buffer of
  `--sim-dev-buffer` (131072) samples emulates the USRP TX buffer in sim. `report.json` -> `latency` (TX buffer,
  switch->air, RX processing/ingest, feedback TX buffer, ACM loop). `--tx-lead-ms 0` = v0.6.0 behaviour.
  v0.6.2: lead default 50 ms; a late source (next sample past its air time, or UHD async "L" via
  `UhdAsyncMonitor`) ends the burst (tx_eob) and starts a new one (tx_sob + tx_time = now + lead) and resends
  the packets of the dropped frames (`--no-tx-resync` = v0.6.1). `TxDeviceSim` follows UHD timing rules (late ->
  "L", drop until EOB, real-time air output); `--sim-stall RATE:MIN_MS:MAX_MS[:gil]` injects source stalls.
  The TX always ends (`TxEndWatchdog`: deadline warm-up + seconds + 30 s, queue stuck 10 s -> END, TX_ABORTED);
  Ctrl+C writes the report.
- `runner_split.py` — TX/RX programs, write `runs/<stamp>_<tx|rx>_<rf|sim>_<mimo>_<mod>/report.json`
  (v0.5: per-second `history`, TX `video` section, `clock` reference). `tools/plot_run.py` plots TX and/or RX reports.

## Commands

```bash
python3 tests/test_modem.py                       # 38 tests (~1 min, needs ffmpeg), must all PASS before committing
bash tools/ci_sim.sh 10 --rate 400e3              # short 2-process sim; CI (.github/workflows/ci.yml) runs both
python3 tools/sim_sweep.py                        # FER / goodput / ACM vs SNR, numpy only -> results/ (~10 min, 16 cores)
python3 tools/link_budget.py                      # per-stage loss (detect/timing/SIG/RS/genie) vs SNR -> results/stage_breakdown.*
python3 tools/sim_sweep.py --fec ldpc             # LDPC part only -> results/ldpc_sweep.csv, ldpc_vs_rs.png (RS CSVs reused)
python3 tools/acm_calibrate.py --out results/acm_thresholds.json   # ACM thresholds per inner code
python3 tools/bench_sim.py --decoder              # LDPC decoder cw/s, Mb/s; bench_sim.py MIMO MOD RATE --fec ldpc [--acm]
python3 tools/rf_fer_curve.py rf_data/<date>      # RF codeword error rate per second vs -EVM + sim curves -> results/rf_<date>.*
# spectrum survey, RECEIVE ONLY (never opens a TX stream; ask the user first, N321 connected, nothing transmitting):
python3 tools/spectrum_survey.py --args addr=192.168.180.95 --gain 50 --seconds 25 --save-iq runs/survey_iq
python3 tools/spectrum_survey.py --iq runs/survey_iq/*.npz   # offline -> results/survey_<date>.png/.json
#   waterfall + power vs time per centre (920.5..922.5 MHz), narrow lines + spacing, burst period, RF0 vs RF1,
#   cleanest 1 MHz video centre outside 922.7 +- 1 MHz
cd OFDM_N321_TX && python3 tools/probe_n321.py --addr 192.168.180.3 --stream   # device + network check, no RF

# two-process simulation on one PC (RX first, then TX in another terminal)
bash OFDM_N321_RX/scripts/sim_rx.sh --play            ; bash OFDM_N321_TX/scripts/sim_tx.sh
bash OFDM_N321_RX/scripts/sim_rx_acm.sh --gui         ; bash OFDM_N321_TX/scripts/sim_tx_acm.sh
bash OFDM_N321_RX/scripts/sim_rx_fbradio.sh --gui     ; bash OFDM_N321_TX/scripts/sim_tx_fbradio.sh

# RF, two PCs (RX PC first)
bash scripts/start_rx.sh --play --gui --freq 921e6 --rx-gain 50                       # in OFDM_N321_RX
bash scripts/start_tx.sh --freq 921e6 --tx-gain 30 --source file --input <video> --seconds 60   # in OFDM_N321_TX
# ACM with radio feedback (RF OK 19/11): add to BOTH sides
#   --mimo siso --acm --fb-link radio   (+ TX: --acm-modcods qpsk,16qam,64qam; fb gains default to the max: RX --fb-tx-gain 30, TX --fb-rx-gain 60)
# v0.5: with --acm the video bitrate follows the modulation (TX --no-adaptive-video to keep 260 kb/s)
python3 tools/plot_run.py <TX report.json> <RX report.json>     # plot_run.png into each run folder
```

Both sides must use the same `--mimo`, `--mod`, `--rate`, `--freq`, `--fec`/`--ldpc-rate` (and `--fb-freq`, `--fb-rate`).
Success = RX prints `RF_RX_VERIFIED` / `SIM_RX_VERIFIED` (video SHA-256 identical).

RX report: `rx.rx_rms` / `snr_db` / `evm_db` = medians over seconds with decoded frames (`median_during_run`);
`*_final` = value at stop (often after the TX ended). v0.6.1: history has `rx_rms_rf0` / `rx_rms_rf1` per port
(RF1 None with radio feedback), `median_during_run.rf1_vs_rf0_db` = branch imbalance (weak RF1 RX2 check);
TX `settings.tx_gain_profile` is recorded. run_*.py stop with an `rm -rf` hint if a stale `OFDM_N321_*/ofdm321/`
folder (only `__pycache__`) shadows the package.

Reading the RX log: `ok` ≈ 481 frames/s (Alamouti/SISO QPSK); `sig/rs/crc` small; `lost` ~0;
QPSK needs EVM < −12 dB, 16QAM < −20 dB; `cond` < 10–15 dB for smux; `rx_rms` 0.005–0.2.
`same_host_queue_to_rx_latency` is meaningless across two PCs.

## Status (2026-11-25, v0.6.2)

- RF loopback (one N321): PASS. RF two N321, Alamouti QPSK, 921 MHz, gain 30/50: success, SNR 26 dB,
  EVM −22 dB, 0 errors while observed. RF1 RX2 branch ~10 dB weaker than RF0 (cond ~31 dB): unresolved.
- RF two N321, SISO + ACM + radio feedback (19/11), fb gains 30/60 (now the defaults): RF_RX_VERIFIED, 0 lost,
  16QAM 89 % of frames, SNR 24 dB, EVM −20 dB. Video was fixed 260 kb/s, so most 16QAM frames were IDLE.
  Run `OFDM_N321_RX/runs/20261119_173835_rx_rf_siso_qpsk` (code v0.4.0): RX gain 50, 921/922.7 MHz, 9624 frames
  (16QAM 8552, QPSK 527, BPSK 545), 35806 codewords ok, rs 1, lost 1, DATA 5185/5185, SNR_eff 23.6 dB.
  rx_rms only 0.00035 (guide 0.005–0.2) yet SNR 24 dB: RX level is low, check before raising modulation.
  Earlier 19/11 RF attempts (16:41, 16:49) FAILED at low SNR/EVM before antennas/gains were settled.
- v0.5 (simulation only so far): video bitrate follows ACM (BPSK 150 / QPSK 300 / 16QAM 600 / 64QAM 900 kb/s TS
  at 1 MS/s); resend on ACM downshift + RX reorder buffer; UDP feedback 0.02 s. Sim steps:30,22,14,22,30:8 at
  1 MS/s: alamouti+UDP and siso+radio both SIM_RX_VERIFIED, 0 missing, 4 bitrate switches; mean TS 525 kb/s vs
  260 fixed. History per second in both reports + tools/plot_run.py; radio feedback watchdog; RF0/RF1 names.
- Done on 2026-11-19 (commits 34e3560..ae8e882, all pushed): CLAUDE.md added; fb gain defaults 30/60 and docs
  "channel A = RF0, B = RF1"; v0.5 in 5 commits (adaptive video, history + plot, watchdog, port names, docs);
  tests 11 -> 18, all PASS (then still 3 identical copies of ofdm321).
- Project report (Claude Doc) updated to v0.5 the same day (v0.4 + v0.5 sections, RF 19/11 row, plot_run.png
  of the alamouti+UDP sim). Its 19/11 RF row still shows "—" for frames, rx_rms and RX gain: the values are
  now known (run above), the doc is not yet updated.
- 2026-11-20: status check, no code change. Git clean (only the local default.json edit), in sync with
  origin/main; 18/18 tests PASS. No v0.5 run on RF yet.
- 2026-11-20 (no hardware): one shared `ofdm321` package at the repo root + `pyproject.toml`;
  tests/ and tools/ at the root (symlinks in the folders); GRC blocks find the shared package; sim_rx/sim_tx and
  fbradio sims SIM_RX_VERIFIED, all 4 GRC flowgraphs compile with grcc.
- 2026-11-20: GitHub CI (tests + 10 s two-process sim) green; RX report rx_rms/snr/evm are medians while
  receiving. tools/sim_sweep.py -> results/ (CSV, PNG, README): alamouti best on Rayleigh (QPSK FER 1% at
  13.3 dB vs siso 32.9); ACM +12-16 % over the best fixed modulation on Rayleigh, but ACM thresholds are too
  conservative on AWGN (0.84-0.90 x best fixed); ACM + smux on Rayleigh loses up to 30 % of codewords; soft RS
  shows no measurable gain on these channels. Fixed: fixed-mode 64QAM soft decoding crashed (1984 bits % 6).
- 2026-11-20 (v0.6, receiver front end): tools/link_budget.py showed BPSK ~ QPSK at low SNR because
  frame detection (threshold 0.6, unused RX1 noise summed in siso), plateau timing and the uncoded SIG failed
  before the data did. Fixed without a frame-format change: detection on used antennas, threshold 0.35, LTF
  fine timing, SIG-less recovery (RS + CRC32 decide). siso AWGN BPSK FER 1 %: 9.4 -> 5.8 dB (genie 5.5), fixed
  channel 6.1 -> 3.1 dB; QPSK unchanged (~9.7). The RX default.json got only the threshold change committed.
- 2026-11-20 (v0.6, LDPC): ofdm321/ldpc.py (802.11n QC-LDPC, 12 codes checked against a dense H), `--fec ldpc`
  `--ldpc-rate`, SIG bit 6 flag, max-log LLR + bit interleaver, per-code ACM thresholds (LDPC 9/12/19/24 dB).
  LDPC 5/6 vs RS at FER 1 %: +1.4..3.9 dB AWGN, +2.3..4.4 dB alamouti Rayleigh, +6.7..13 dB siso Rayleigh;
  ACM + LDPC +13 % mean goodput (same frame capacity). 1 MS/s real time OK (smux 64QAM tightest, x1.17 fixed 5/6,
  x1.68 ACM). Sims PASS: --fec ldpc fixed, ACM UDP, siso ACM radio; CI runs an LDPC sim too. 27 tests.
- 2026-11-23 RF (v0.6.0, data in rf_data/20261123, not committed): siso QPSK RF_RX_VERIFIED (SNR 23, EVM -20);
  siso 16QAM/64QAM fixed FAILED (rs_fail 28 % / 48 %: gain steps down to -EVM 9-13 / 12-21 dB; per second the
  RF FER vs -EVM matches the sim within 1-2 dB, tools/rf_fer_curve.py -> results/rf_20261123.*); alamouti QPSK verified
  (SNR 34); siso ACM + radio feedback FAILED (920 missing, all in a 45-60 s step at SNR_eff ~5.7 dB) with loop
  0.398 s and BPSK<->QPSK chatter (75-90 s) at SNR_eff 18.5 dB.
- v0.6.1 latency: sim with the USRP buffer emulation reproduced the loop (428 ms: TX video buffer 135 + feedback TX
  buffer 267 ms); `--tx-lead-ms 30` (default) -> 78 ms (buffers 25 + 24 ms). Not yet verified on RF.
- v0.6.1 ACM debounce: test replay of 18.5 dB + 14.3 dB dips: 41 -> 0 switches; sim `--snr-profile
  dips:19:9:0.8:15`: 33 -> 1 switches, 0 lost both, DATA 4672 -> 7987 (video stays at the QPSK rate).
- 2026-11-25 RF (v0.6.1, lead 30, ACM + radio feedback, run OFDM_N321_RX/runs/20261125_175635_rx_rf_siso_qpsk):
  after ~55 s TX printed "LLLL", nothing on air, TX did not stop; ACM stayed at BPSK although the RX reported
  SNR_eff 21 dB. Cause: gr-uhd stamps every packet; a stall > lead makes packets late and the radio drops all
  until an EOB (never sent). Reproduced in sim (TxDeviceSim with UHD rules + --sim-stall 0.5:20:150:gil: FAIL,
  0 DATA, ACM 0 switches); v0.6.2 fix: SIM_RX_VERIFIED, 0 missing, loop 124.5 ms, ACM normal. Why the TX ACM got no
  usable feedback from the very start of that run is not proven (no TX report); the new fb_rx_* history fields
  and the "Phản hồi radio yếu" warning will show it on the next RF run.

## Next steps (agreed direction)

1. First RF run of v0.5 (`--mimo siso --acm --fb-link radio`, adaptive video); check ffplay at bitrate switches,
   TX queue/backlog after downshifts, `plot_run.py` plots.
2. Fix/diagnose the weak RF1 RX2 branch (swap antennas).
3. Systematic measurements script (loss/EVM vs gain and mode, ACM vs fixed), plots for the report.
4. Better SNR_eff estimate (noise from 18 null samples per frame smears the ACM CER curves; LDPC thresholds
   only 1-2 dB lower although FER gains are 2-4 dB); first RF run with `--fec ldpc`; GPSDO sync; later a
   DVB-S2-style single-carrier modem.

Docs: `OFDM_N321_*/docs/` — `THIET_KE.md`, `HUONG_DAN_2_N321.md`, `GIAI_MEM_INTERLEAVING.md`, `ACM.md`,
`PHAN_HOI_RADIO.md`, `MOT_CONG_MANG.md`, `LDPC.md`. Full project report (Vietnamese) is a Claude Doc:
https://claude.ai/artifact/FYd3drF6E8f9nVxDoSjdiX

## Conventions

- Keep the real-time budget: no per-sample Python loops in `work()`; batch with numpy.
- New feature → simulate first (ZeroMQ two-process), add a test in `tests/test_modem.py`, then RF.
- Commit messages in English; never commit `runs/`, `__pycache__`, video files.
