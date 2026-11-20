# grc/ofdm_n321_rx.grc — máy thu (N321 #2)

Sơ đồ GNU Radio Companion 3.10.9 của máy thu. Máy phát dùng `OFDM_N321_TX/grc/ofdm_n321_tx.grc`.
Chạy máy thu **trước** máy phát.

Mở bằng lệnh:

```bash
gnuradio-companion ~/Desktop/MIMO-OFDM-USRP-main/OFDM_N321_RX/grc/ofdm_n321_rx.grc
```

| Khối | Loại | Việc làm |
|---|---|---|
| UHD: USRP Source | Có sẵn | N321 #2 `192.168.180.95`, `A:0 B:0`, cổng RX2, gain 35 |
| [E2] MIMO OFDM RX | Embedded (`ofdm321.blocks.MimoRxSink`) | Đồng bộ Schmidl-Cox, bù CFO và lệch clock, ước lượng H 2×2, cân bằng MRC/SFBC/MMSE, de-interleave, giải mềm RS, kiểm tra CRC32, xuất PDU |
| [E3] Video PDU receiver | Embedded (`ofdm321.blocks.VideoReceiver`) | Tự bám session của máy phát, RS8+8 có kiểm chứng, ghi `rx_ts_path` và gửi UDP 5002 |
| QT GUI Frequency Sink | Có sẵn | Phổ thu tại RF0 RX2 và RF1 RX2 (kênh A, B) (cột trái cửa sổ) |
| PDU to Tagged Stream → QT GUI Constellation Sink | Có sẵn | **Chòm sao sau cân bằng** (cột phải): lấy từ cổng `const` của [E2], 1 khung tốt mỗi `const_every` = 8 khung |
| ZMQ PULL + Deinterleave | Có sẵn, **tắt sẵn** | Mô phỏng: nhận mẫu từ máy phát |
| [E5] SFO resampler, [E4] kênh 2×2 | Embedded, **tắt sẵn** | Mô phỏng hai máy độc lập: lệch clock 5 ppm, CFO 2 kHz, kênh Rayleigh 20 dB |

**Chòm sao.** [E2] có thêm cổng message `const`: ký hiệu dữ liệu sau MRC/SFBC/MMSE và bù pha pilot,
trước khi quyết định bit (QPSK 992 điểm/khung, 16QAM 496; smux gồm cả hai luồng). Đặt `Const_Every = 0`
để tắt. Ảnh chạy mô phỏng: `../docs/img/grc_rx_gui_alamouti.png`. Cách đọc chòm sao: xem
`docs/HUONG_DAN_2_N321.md`, mục 6.

Các khối [E] dùng gói chung `ofdm321` ở gốc repo. Nếu đã cài (`pip install -e .`, có trong
`scripts/setup_ubuntu.sh`) thì chạy ở đâu cũng được. Nếu chưa cài, khối tự tìm theo thứ tự: biến môi trường
`OFDM_N321_DIR` (đường dẫn gốc repo), rồi các thư mục cha của file sinh ra từ `.grc`, rồi `~/MIMO-OFDM-USRP`
và `~/Desktop/MIMO-OFDM-USRP[-main]`.

Xem video thu trong khi chạy: `ffplay -fflags nobuffer udp://127.0.0.1:5002`. Khi dừng, khối [E2] in thống kê
`ok / soft / sig / rs / crc`, SNR, EVM, cond; khối [E3] in số gói DATA, số gói thiếu và đã nhận END chưa.

**Mô phỏng:**

1. Disable UHD Source, enable 4 khối SIM.
2. Nối [E4] tới [E2] và tới khối phổ (thay cho UHD Source). Nhánh chòm sao giữ nguyên.
3. Chạy sơ đồ này trước, rồi mới chạy máy phát ở chế độ mô phỏng.
