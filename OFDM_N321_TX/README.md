# OFDM_N321_TX — máy PHÁT (N321 #1)

Phát video OFDM 2×2 MIMO bằng 2 anten trên **TX/RX A:0** và **TX/RX B:0**. Máy thu dùng folder `OFDM_N321_RX`.
Hướng dẫn đầy đủ (cách nối mạng, đổi IP, anten, đọc log): **[docs/HUONG_DAN_2_N321.md](docs/HUONG_DAN_2_N321.md)**.

```bash
bash scripts/setup_ubuntu.sh                              # một lần
python3 tools/probe_n321.py --addr 192.168.180.3 --stream # không phát RF
python3 tests/test_modem.py

# máy RX chạy start_rx.sh TRƯỚC, sau đó:
bash scripts/start_tx.sh                                               # testpattern 30 s
bash scripts/start_tx.sh --source file --input ~/Videos/clip.mp4 --seconds 60
bash scripts/start_tx.sh --source webcam --input /dev/video0
bash scripts/start_tx.sh --mimo smux --freq 921e6                      # RX phải dùng cùng tham số

bash scripts/sim_tx.sh                                    # mô phỏng qua ZeroMQ (RX chạy sim_rx.sh trước)
```

Mặc định: alamouti QPSK, 1 MS/s, 915 MHz, `tx_gain` 20 (tối đa 30 khi dùng anten), N321 ở `192.168.180.3`
(sửa trong `config/default.json` hoặc dùng `--args`). Kết thúc lượt chạy, chương trình in `TX_COMPLETE`, session và
sha256 của video; `runs/<lượt>/report.json` dùng để so sánh với RX.

## v0.2: giải mềm và interleaving

Bộ thu có giải mềm RS (xóa byte theo độ tin cậy); bộ phát có interleaver `block` mặc định, bộ thu tự theo.
Cập nhật **cả hai** folder TX và RX cùng lúc. Chi tiết và số đo: [docs/GIAI_MEM_INTERLEAVING.md](docs/GIAI_MEM_INTERLEAVING.md).
