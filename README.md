# MIMO-OFDM-USRP

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
| [`OFDM_N321_TX`](OFDM_N321_TX) | N321 **#1** phát (anten trên TX/RX A, B) | `bash scripts/start_tx.sh --source file --input video.mp4` |
| [`OFDM_N321_RX`](OFDM_N321_RX) | N321 **#2** thu (anten trên RX2 A, B) | `bash scripts/start_rx.sh --play` (chạy trước TX) |

Mỗi folder chạy độc lập và có bản sao riêng của gói `ofdm321`.

## Tính năng

- 4 chế độ: `siso`, `simo` (MRC), `alamouti` (SFBC 2×2, mặc định), `smux` (2 luồng, MMSE); điều chế BPSK, QPSK, 16QAM, 64QAM.
- **Điều chế thích ứng (ACM, v0.3):** máy phát chọn điều chế cho từng khung theo SNR máy thu báo về qua UDP ([ACM.md](OFDM_N321_RX/docs/ACM.md)).
- Máy thu có cửa sổ `--gui`: chòm sao sau cân bằng và phổ 2 kênh.
- Khung OFDM: FFT 64, CP 16, 48 sóng mang dữ liệu + 4 pilot. Mỗi khung có STF (dùng CSD), 2 LTF ma trận P và SIG có CRC16.
- Bù CFO, lệch clock (SFO) và pha chung (CPE) trên từng khung, nên chạy được giữa hai máy có clock độc lập.
- FEC: CRC32 + RS(248,216) trong, có **giải mềm** errors-and-erasures; interleaver `block` / `carrier`; RS8+8 ngoài,
  chỉ giao dữ liệu đã kiểm chứng.
- Mô phỏng không cần radio: kênh 2×2 Rayleigh hoặc đa đường, AWGN, CFO, SFO; TX và RX hai tiến trình nối qua ZeroMQ.

## Bắt đầu nhanh (Ubuntu 24.04)

```bash
cd OFDM_N321_MIMO
bash scripts/setup_ubuntu.sh
python3 tests/test_modem.py                          # tự kiểm tra modem
bash scripts/run_sim.sh --seconds 20 --play          # mô phỏng, không cần radio
python3 tools/probe_n321.py --addr <IP N321> --stream   # kiểm tra N321, không phát RF
```

Hướng dẫn chi tiết:

- [Thiết kế](OFDM_N321_MIMO/docs/THIET_KE.md)
- [Đấu nối 2×2](OFDM_N321_MIMO/docs/DAU_NOI_2x2.md)
- [Chạy hai N321](OFDM_N321_TX/docs/HUONG_DAN_2_N321.md)
- [Giải mềm và interleaving](OFDM_N321_TX/docs/GIAI_MEM_INTERLEAVING.md)
- [Điều chế thích ứng (ACM)](OFDM_N321_RX/docs/ACM.md)

## Trạng thái

| Hạng mục | Kết quả |
|---|---|
| Loopback RF trên một N321, anten 915 MHz, Alamouti (16/11/2026) | PASS: 3455/3455 gói, video trùng SHA-256, SNR 33–35 dB |
| Mô phỏng hai N321 (CFO đến 5 kHz, SFO đến 10 ppm) | PASS ở siso, alamouti, smux |
| Giải mềm + interleaving (v0.2), mô phỏng | SISO 18 dB: mất khung 38% → 25%; smux 20 dB: 40% → 29% |
| ACM (v0.3), mô phỏng SNR 30→11→28 dB | Mất 1,3% gói (QPSK cố định 0%, 16QAM cố định 47%), dung lượng TB +71% so với QPSK |
| RF giữa hai N321 | Chưa chạy |

## An toàn RF

- Khi chạy qua cáp, bắt buộc có attenuator ≥ 30 dB (`--attenuation-db`).
- Khi dùng anten, `tx_gain` tối đa 30. Kiểm tra quy định băng tần nơi phát trước (ở Việt Nam thường là 920–923 MHz, đổi bằng `--freq`).
