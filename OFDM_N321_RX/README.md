# OFDM_N321_RX — máy THU (N321 #2)

Thu video OFDM 2×2 MIMO bằng 2 anten trên **RX2 A:0** và **RX2 B:0**. Máy phát dùng folder `OFDM_N321_TX`.
Hướng dẫn đầy đủ: **[docs/HUONG_DAN_2_N321.md](docs/HUONG_DAN_2_N321.md)**.

```bash
bash scripts/setup_ubuntu.sh                              # một lần
python3 tools/probe_n321.py --addr 192.168.180.4 --stream # không phát RF
python3 tests/test_modem.py

bash scripts/start_rx.sh --play           # chạy TRƯỚC máy TX; tự dừng ~3 s sau khi nhận END
bash scripts/start_rx.sh --mimo smux --freq 921e6 --play   # phải trùng tham số bên TX
bash scripts/start_rx.sh --play --gui     # thêm cửa sổ chòm sao sau cân bằng + phổ 2 kênh
bash scripts/start_rx.sh --acm --acm-host <IP PC phát> --play --gui   # điều chế thích ứng (docs/ACM.md)
python3 tools/compare_tx_rx.py <đường dẫn report.json của TX>   # video thu có giống hệt video phát không
bash scripts/play_latest.sh               # xem lại video đã thu

bash scripts/sim_rx.sh --play             # mô phỏng hai radio (CFO 2 kHz, lệch clock 5 ppm) qua ZeroMQ
```

Mặc định: alamouti QPSK, 1 MS/s, 915 MHz, `rx_gain` 35, N321 ở `192.168.180.4`
(sửa `config/default.json` hoặc dùng `--args addr=...,master_clock_rate=200e6`).
RX tự bám session của TX đầu tiên nó nghe được (`--session N` để chỉ nhận một session cụ thể).
Thành công khi in `RF_RX_VERIFIED`: nhận được END, không thiếu DATA và video giải mã được.

## v0.2: giải mềm và interleaving

Bộ thu có giải mềm RS (xóa byte theo độ tin cậy); bộ phát có interleaver `block` mặc định, bộ thu tự theo.
Cập nhật **cả hai** folder TX và RX cùng lúc. Chi tiết và số đo: [docs/GIAI_MEM_INTERLEAVING.md](docs/GIAI_MEM_INTERLEAVING.md).
