# grc/ofdm_n321_tx.grc — máy phát (N321 #1)

Sơ đồ GNU Radio Companion 3.10.9 của máy phát. Máy thu dùng `OFDM_N321_RX/grc/ofdm_n321_rx.grc`.
Sơ đồ tổng quan cả hai máy là `MIMO-OFDM-USRP/grc/ofdm_n321_2may.grc`.

Mở bằng lệnh:

```bash
gnuradio-companion ~/Desktop/MIMO-OFDM-USRP-main/OFDM_N321_TX/grc/ofdm_n321_tx.grc
```

| Khối | Loại | Việc làm |
|---|---|---|
| [E1] MIMO OFDM TX source | Embedded (`ofdm321.blocks.MimoTxSource`) | Đọc TS từ `ts_path`, đóng gói DATA/RS8_PARITY/END, mã CRC32 + RS(248,216), xáo trộn, interleaver, điều chế khung OFDM 2×2 (STF có CSD, 2 LTF ma trận P, SIG, pilot). Khi hàng đợi rỗng phát khung IDLE |
| UHD: USRP Sink | Có sẵn | N321 #1 `192.168.180.3`, `A:0 B:0`, cổng TX/RX, 915 MHz, gain 20 |
| Interleave + ZMQ PUSH Sink | Có sẵn, **tắt sẵn** | Chỉ dùng khi mô phỏng: gửi 2 kênh mẫu tới máy thu qua `tcp://127.0.0.1:5560` |

Các khối [E] dùng gói chung `ofdm321` ở gốc repo. Nếu đã cài (`pip install -e .`, có trong
`scripts/setup_ubuntu.sh`) thì chạy ở đâu cũng được. Nếu chưa cài, khối tự tìm theo thứ tự: biến môi trường
`OFDM_N321_DIR` (đường dẫn gốc repo), rồi các thư mục cha của file sinh ra từ `.grc`, rồi `~/MIMO-OFDM-USRP`
và `~/Desktop/MIMO-OFDM-USRP[-main]`.

**Hai máy phải trùng** `samp_rate`, `center_freq`, `mimo_mode`, `modulation`. Riêng `interleaver` chỉ cần chọn ở máy phát, máy thu tự đọc từ khung.

Muốn phát video: đặt biến `ts_path` trỏ tới một file `.ts` (hoặc một FIFO do FFmpeg ghi vào). Để trống thì chỉ phát khung IDLE, máy thu vẫn khóa được để kiểm tra link.

Sơ đồ không có kiểm tra an toàn RF như `scripts/start_tx.sh`. Khi dùng anten, giữ `tx_gain` ≤ 30.

**Mô phỏng:**

1. Disable UHD Sink, enable Interleave và ZMQ PUSH, đặt `throttle=True` ở [E1].
2. Chạy máy thu (chế độ mô phỏng) trước, rồi mới chạy sơ đồ này.

Đã thử hai tiến trình trên GNU Radio 3.10.9: nhận đủ 717/717 gói, video trùng từng byte.
