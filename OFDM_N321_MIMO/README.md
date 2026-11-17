# OFDM Video N321 — 2×2 MIMO loopback (v0.1)

Bản nâng cấp của `OFDM_Video_N210` lên **một USRP N321**, nối với máy Ubuntu qua **10 GbE**,
chạy ở **915 MHz** và dùng cả 2 kênh TX lẫn 2 kênh RX.

- Tầng video, đóng gói và FEC (CRC32 + RS(248,216) + RS8+8 có kiểm chứng) giữ nguyên byte như v1.23.
- Tầng vật lý là modem OFDM MIMO mới, có 4 chế độ: `siso`, `simo` (MRC), `alamouti` (SFBC, mặc định)
  và `smux` (2 luồng, MMSE).

Xem [docs/THIET_KE.md](docs/THIET_KE.md) cho thiết kế và [docs/DAU_NOI_2x2.md](docs/DAU_NOI_2x2.md) cho cách đấu nối.

## Cài đặt (Ubuntu 24.04)

```bash
bash scripts/setup_ubuntu.sh                       # GNU Radio 3.10, UHD, FFmpeg, image N320/N321
sudo bash scripts/setup_network_10g.sh <iface>     # IP 192.168.20.1/24, MTU 9000, socket buffer 32 MB
python3 tools/probe_n321.py --stream               # không phát RF: kiểm tra UHD/MPM, MTU, overrun
```

Máy Ubuntu này đã có UHD 4.6 (đã dùng cho N210). Filesystem/FPGA trên N321 phải **cùng phiên bản UHD**.
Nếu `uhd_usrp_probe` báo lệch phiên bản (compat mismatch), cần cập nhật N321 trước.

## Chạy

```bash
python3 tests/test_modem.py                              # tự kiểm tra modem
python3 tools/bench_sim.py alamouti qpsk 1e6 --no-channel # đo CPU còn dư bao nhiêu
bash scripts/run_sim.sh --seconds 20 --play              # mô phỏng kênh 2x2 Rayleigh, không cần radio
bash scripts/run_cable_2x2.sh --attenuation-db 30 --mimo siso --play   # N321, cáp thẳng có attenuator
bash scripts/run_cable_2x2.sh --attenuation-db 30 --mimo alamouti --play
bash scripts/run_antenna_915.sh --mimo alamouti --play   # 4 anten; kiểm tra quy định băng tần trước
```

Tùy chọn: `--mimo siso|simo|alamouti|smux`, `--mod qpsk|16qam`, `--rate 500000|1e6|2e6`, `--freq`,
`--tx-gain`, `--rx-gain`, `--source testpattern|webcam|file --input ...`, `--config config/light_500k.json`
(dùng khi CPU không kịp), `--config config/smux_qpsk.json` (TS 420 kb/s).

Kết quả của mỗi lượt nằm trong `runs/<thời gian>_.../`: `report.json`, `video_tx.ts`, `video_rx.ts`.
PASS khi TS thu trùng SHA-256 với TS phát, không thiếu DATA và đã nhận END.

## Trạng thái

- Chế độ mô phỏng: đã kiểm chứng trên GNU Radio 3.10.9, cả 4 chế độ đều PASS với video thật.
- Trên N321 thật: **chưa chạy**. Cần đi qua các bước bring-up trong THIET_KE.md mục 11.

Sơ đồ GNU Radio Companion để xem tổng quan: `grc/ofdm_n321_mimo.grc` (xem `grc/README_GRC.md`).
