# MIMO-OFDM-USRP

[![CI](https://github.com/cuogrec/MIMO-OFDM-USRP/actions/workflows/ci.yml/badge.svg)](https://github.com/cuogrec/MIMO-OFDM-USRP/actions/workflows/ci.yml)

Truyền video thời gian thực bằng **OFDM 2×2 MIMO** trên **USRP N321**, viết bằng Python, GNU Radio 3.10 và UHD 4.6.
Hệ thống kế thừa tầng video, đóng gói và FEC của dự án OFDM video trên USRP N210.

```
FFmpeg (H.264 → MPEG-TS) → gói 212 B + RS8+8 → CRC32 + RS(248,216) → interleaver → OFDM MIMO (2 TX)
        ~~~ kênh 2×2: anten 915 MHz, cáp có attenuator, hoặc ZeroMQ (mô phỏng) ~~~
2 RX → Schmidl-Cox + CFO → ước lượng H 2×2 (LTF) → MRC / SFBC / MMSE → giải mềm RS → RS8+8 kiểm chứng → video
```

## Thư mục

| Folder | Dùng cho | Lệnh chính |
|---|---|---|
| [`OFDM_N321_MIMO`](OFDM_N321_MIMO) | Loopback trên **một** N321 (2 TX + 2 RX) | `bash scripts/run_antenna_915.sh --mimo alamouti --play` |
| [`OFDM_N321_TX`](OFDM_N321_TX) | N321 **#1** phát (anten trên RF0 TX/RX, RF1 TX/RX) | `bash scripts/start_tx.sh --source file --input video.mp4` |
| [`OFDM_N321_RX`](OFDM_N321_RX) | N321 **#2** thu (anten trên RF0 RX2, RF1 RX2) | `bash scripts/start_rx.sh --play` (chạy trước TX) |

Mã xử lý tín hiệu là **một gói Python chung** `ofdm321/` ở gốc repo. Ba folder chỉ giữ phần riêng của từng
máy: `scripts/`, `config/` (IP N321, gain...), `docs/`, `grc/` và chương trình chạy `run_*.py`.

```
MIMO-OFDM-USRP/
├── ofdm321/          gói chung: modem OFDM, FEC, ACM, khối GNU Radio, chương trình TX/RX
├── tests/            python3 tests/test_modem.py
├── tools/            probe_n321.py, plot_run.py, make_iq.py, ...
├── pyproject.toml    pip install -e .  (scripts/setup_ubuntu.sh làm sẵn)
├── grc/              sơ đồ GRC tổng quan hai máy
├── OFDM_N321_TX/     máy phát: scripts/ config/ docs/ grc/ run_tx.py   (tools, tests: symlink về gốc)
├── OFDM_N321_RX/     máy thu:  scripts/ config/ docs/ grc/ run_rx.py
└── OFDM_N321_MIMO/   loopback: scripts/ config/ docs/ grc/ run_n321.py
```

`run_*.py` tự tìm gói ở gốc repo nếu chưa cài, nên các lệnh `bash scripts/...` chạy được ngay sau `git clone`.
`config/default.json` và `runs/` vẫn thuộc riêng từng folder. Cổng được gọi theo nhãn trên mặt máy N321:
**RF0** = kênh A (UHD `A:0`), **RF1** = kênh B (`B:0`).

## Tính năng

- 4 chế độ: `siso`, `simo` (MRC), `alamouti` (SFBC 2×2, mặc định), `smux` (2 luồng, MMSE); điều chế BPSK, QPSK, 16QAM, 64QAM.
- **Điều chế thích ứng (ACM, v0.3):** máy phát chọn điều chế cho từng khung theo SNR máy thu báo về qua UDP ([ACM.md](OFDM_N321_RX/docs/ACM.md)).
- **Phản hồi qua sóng radio (v0.4):** chạy SISO trên RF0, bên thu gửi SNR về bên phát trên RF1 của hai N321
  ([PHAN_HOI_RADIO.md](OFDM_N321_RX/docs/PHAN_HOI_RADIO.md)).
- **Bitrate video theo ACM (v0.5):** một FFmpeg mã hóa mỗi điều chế một bản (BPSK 150 … 64QAM 900 kb/s TS ở 1 MS/s),
  máy phát chuyển bản tại khung khóa; phát lại khi hạ điều chế + bộ đệm sắp xếp lại ở máy thu; lịch sử mỗi giây
  trong `report.json` và `tools/plot_run.py` để vẽ đồ thị.
- **Bộ thu nhạy hơn ở SNR thấp (v0.6):** dò khung trên các anten đang dùng, định thời tinh bằng LTF, vẫn giải khung khi SIG
  hỏng (RS + CRC32 quyết định); BPSK siso AWGN đạt FER 1% ở 5,8 dB thay vì 9,4 dB (`--legacy-sync` = bộ thu cũ).
- Máy thu có cửa sổ `--gui`: chòm sao sau cân bằng và phổ 2 kênh.
- Khung OFDM: FFT 64, CP 16, 48 sóng mang dữ liệu + 4 pilot. Mỗi khung có STF (dùng CSD), 2 LTF ma trận P và SIG có CRC16.
- Bù CFO, lệch clock (SFO) và pha chung (CPE) trên từng khung, nên chạy được giữa hai máy có clock độc lập.
- FEC: CRC32 + RS(248,216) trong, có **giải mềm** errors-and-erasures; interleaver `block` / `carrier`; RS8+8 ngoài,
  chỉ giao dữ liệu đã kiểm chứng.
- **LDPC 802.11n làm mã trong (v0.6, tùy chọn `--fec ldpc --ldpc-rate 1/2|2/3|3/4|5/6`, hai bên phải giống nhau):**
  giải min-sum theo lớp, LLR mềm max-log, interleaver bit ([LDPC.md](OFDM_N321_RX/docs/LDPC.md)). Mặc định vẫn RS.
- Mô phỏng không cần radio: kênh 2×2 Rayleigh hoặc đa đường, AWGN, CFO, SFO; TX và RX hai tiến trình nối qua ZeroMQ.

## Bắt đầu nhanh (Ubuntu 24.04)

```bash
cd OFDM_N321_MIMO
bash scripts/setup_ubuntu.sh                         # apt + pip install -e . (gói ofdm321 chung)
python3 tests/test_modem.py                          # tự kiểm tra modem (= ../tests/test_modem.py)
bash scripts/run_sim.sh --seconds 20 --play          # mô phỏng, không cần radio
python3 tools/probe_n321.py --addr <IP N321> --stream   # kiểm tra N321, không phát RF
```

Hướng dẫn chi tiết:

- [Thiết kế](OFDM_N321_MIMO/docs/THIET_KE.md)
- [Đấu nối 2×2](OFDM_N321_MIMO/docs/DAU_NOI_2x2.md)
- [Chạy hai N321](OFDM_N321_TX/docs/HUONG_DAN_2_N321.md)
- [Giải mềm và interleaving](OFDM_N321_TX/docs/GIAI_MEM_INTERLEAVING.md)
- [Điều chế thích ứng (ACM)](OFDM_N321_RX/docs/ACM.md)
- [Phản hồi ACM qua sóng radio, không cần mạng giữa hai PC](OFDM_N321_RX/docs/PHAN_HOI_RADIO.md)
- [PC chỉ có một cổng mạng: N321 #1 tự phát file IQ](OFDM_N321_TX/docs/MOT_CONG_MANG.md)
- [Đo đặc trưng hệ thống bằng mô phỏng: FER, thông lượng, ACM theo SNR](results/README.md)
- [LDPC làm mã trong](OFDM_N321_RX/docs/LDPC.md)

## Trạng thái

| Hạng mục | Kết quả |
|---|---|
| Loopback RF trên một N321, anten 915 MHz, Alamouti (16/11/2026) | PASS: 3455/3455 gói, video trùng SHA-256, SNR 33–35 dB |
| Mô phỏng hai N321 (CFO đến 5 kHz, SFO đến 10 ppm) | PASS ở siso, alamouti, smux |
| Giải mềm + interleaving (v0.2), mô phỏng | SISO 18 dB: mất khung 38% → 25%; smux 20 dB: 40% → 29% |
| ACM (v0.3), mô phỏng SNR 30→11→28 dB | Mất 1,3% gói (QPSK cố định 0%, 16QAM cố định 47%), dung lượng TB +71% so với QPSK |
| RF hai N321, Alamouti QPSK, 921 MHz, anten (17/11/2026) | `RF_RX_VERIFIED`, SNR 26 dB, EVM −22 dB, 0 lỗi |
| RF hai N321, SISO + ACM + phản hồi radio, 921/922,7 MHz, fb gain 30/60 (19/11/2026) | `RF_RX_VERIFIED`, 0 gói mất, 16QAM 89% khung, SNR 24 dB, EVM −20 dB; video cố định 260 kb/s nên dư dung lượng |
| Bitrate theo ACM (v0.5), mô phỏng SNR 30→22→14→22→30 dB, 1 MS/s | UDP (alamouti) và radio (siso): `SIM_RX_VERIFIED`, thiếu 0 gói; TS trung bình 525 kb/s (alamouti) so với 260 kb/s cố định |
| Bitrate theo ACM trên RF | Chưa chạy |
| Bộ thu v0.6 ở SNR thấp, mô phỏng (`tools/link_budget.py`) | FER bám giới hạn dữ liệu (genie), chênh ≤ 0,5 dB; BPSK siso AWGN 9,4 → 5,8 dB, kênh cố định 6,1 → 3,1 dB |

## An toàn RF

- Khi chạy qua cáp, bắt buộc có attenuator ≥ 30 dB (`--attenuation-db`).
- Khi dùng anten, `tx_gain` tối đa 30. Kiểm tra quy định băng tần nơi phát trước (ở Việt Nam thường là 920–923 MHz, đổi bằng `--freq`).
