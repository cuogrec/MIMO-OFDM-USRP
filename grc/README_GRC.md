# grc/ofdm_n321_2may.grc — tổng quan hệ thống hai USRP N321

Sơ đồ GNU Radio Companion (3.10.9) của bản v0.2: **N321 #1 phát, N321 #2 thu**. Cả hai máy nối qua switch 1 GbE
tới cùng một PC Ubuntu, mỗi máy chạy clock riêng.

Chương trình chạy chính thức vẫn là `scripts/start_rx.sh` và `scripts/start_tx.sh`. Hai script đó có kiểm tra an toàn RF,
xuất `report.json` và so sánh SHA-256. File `.grc` này dùng để **xem tổng quan** và thử nhanh.

## Cách mở

1. Chép thư mục `grc/` vào gốc repo: `~/Desktop/MIMO-OFDM-USRP-main/grc/`.
2. Mở file: `gnuradio-companion ~/Desktop/MIMO-OFDM-USRP-main/grc/ofdm_n321_2may.grc`.

Các khối [E] tìm gói `ofdm321` theo thứ tự sau:

1. biến môi trường `OFDM_N321_DIR`;
2. thư mục `../OFDM_N321_RX` hoặc `../OFDM_N321_TX` tính từ file `.grc`;
3. `~/Desktop/MIMO-OFDM-USRP-main/OFDM_N321_RX`.

Nếu không tìm thấy, khối vẫn hiện đúng cổng nhưng có tên `[THIẾU ofdm321]` và báo lỗi khi bấm chạy.

## Các khối

| Khối | Loại | Class gốc trong `ofdm321` | Việc làm |
|---|---|---|---|
| [E1] MIMO OFDM TX source | Embedded | `blocks.MimoTxSource` + phần nạp TS | Đọc TS từ `ts_path` (file hoặc FIFO), đóng gói DATA/RS8_PARITY/END, mã CRC32 + RS(248,216), xáo trộn, **interleaver**, điều chế khung OFDM 2×2, xuất 2 luồng. Khi hàng đợi rỗng phát khung IDLE |
| UHD: USRP Sink | Có sẵn (gr-uhd) | — | N321 #1 `192.168.180.3`, subdev `A:0 B:0`, cổng TX/RX, 915 MHz, gain 20 |
| UHD: USRP Source | Có sẵn (gr-uhd) | — | N321 #2 `192.168.180.95`, subdev `A:0 B:0`, cổng RX2, gain 35 |
| [E2] MIMO OFDM RX | Embedded | `blocks.MimoRxSink` + `modem.Demodulator` | Đồng bộ Schmidl-Cox, bù CFO, ước lượng H 2×2, cân bằng MRC/SFBC/MMSE, **de-interleave**, **giải mềm RS**, kiểm tra CRC32, xuất PDU |
| [E3] Video PDU receiver | Embedded | `blocks.VideoReceiver` | Tự bám session của máy phát (`session=0`), RS8+8 có kiểm chứng, ghi `video_rx.ts` và gửi UDP 5002 cho ffplay |
| [E4] 2×2 channel simulator | Embedded, tắt sẵn | `blocks.MimoChannelSim` | Mô phỏng kênh 2×2 Rayleigh, SNR 20 dB, CFO 2 kHz |
| [E5] SFO resampler | Embedded, tắt sẵn | `blocks.SfoResampler` | Mô phỏng lệch clock 5 ppm giữa hai máy |
| QT GUI Frequency Sink | Có sẵn | — | Phổ thu tại RX2 A và RX2 B |
| PDU to Tagged Stream → QT GUI Constellation Sink | Có sẵn | cổng `const` của `blocks.MimoRxSink` | **Chòm sao** ký hiệu dữ liệu sau cân bằng (MRC/SFBC/MMSE), 1 khung tốt mỗi 8 khung (`const_every`) |

Các khối OFDM có sẵn của GNU Radio (`ofdm_tx/rx`, `header_payload_demux`…) chỉ hỗ trợ một anten, nên toàn bộ
modem MIMO được đặt trong [E1] và [E2].

## Tham số (khối Variable)

| Biến | Mặc định | Ghi chú |
|---|---|---|
| `mimo_mode`, `modulation` | `alamouti`, `qpsk` | Phát và thu dùng chung một biến nên luôn khớp nhau |
| `interleaver` | `block` | Máy phát chọn, máy thu tự đọc từ trường SIG |
| `soft_decoding` | `True` | Đặt `False` để so sánh với giải cứng |
| `tx_args`, `rx_args` | `.3` và `.95`, `master_clock_rate=200e6` | IP của hai N321 |
| `tx_gain`, `rx_gain` | 20 / 35 | Khi dùng anten, giữ `tx_gain` ≤ 30 |
| `ts_path` | trống | Để trống thì chỉ phát khung IDLE. Muốn có video, trỏ tới một file `.ts` |

Sơ đồ này **không** có kiểm tra an toàn RF như `start_tx.sh` (giới hạn gain, attenuator). Khi sửa tham số cần tự giữ các giới hạn đó.

## Chạy mô phỏng (không cần radio)

1. Disable hai khối UHD, enable [E5] và [E4].
2. Nối [E4] tới [E2] và tới khối phổ; đặt `throttle=True` ở [E1].
3. Đặt `ts_path` trỏ tới một file `.ts`.

Đã kiểm tra trên GNU Radio 3.10.9: mô phỏng với lệch clock 5 ppm, CFO 2 kHz, kênh Rayleigh 20 dB nhận đủ 717/717 gói DATA,
file video thu trùng từng byte với file phát. Bản dùng hai N321 thật chưa chạy bằng file `.grc` này.
