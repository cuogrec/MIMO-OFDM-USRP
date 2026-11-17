# grc/ofdm_n321_mimo.grc

Sơ đồ GNU Radio Companion (3.10.9) để **xem tổng quan** và thử nhanh project. Chương trình chính vẫn là
`run_n321.py`: nó có timed start chính xác, kiểm tra an toàn RF, encoder FFmpeg và xuất `report.json`.

| Khối | Class gốc | Việc làm |
|---|---|---|
| [E1] MIMO OFDM TX source | `ofdm321.blocks.MimoTxSource` + phần nạp TS | Đọc TS từ `ts_path` (file hoặc FIFO), đóng gói DATA/RS8_PARITY/END, điều chế khung MIMO, xuất 2 luồng |
| UHD USRP Sink | gr-uhd | N321, `A:0 B:0`, cổng TX/RX, 915 MHz, sync PC clock |
| UHD USRP Source | gr-uhd | N321, `A:0 B:0`, cổng RX2 |
| [E2] MIMO OFDM RX | `ofdm321.blocks.MimoRxSink` | Đồng bộ, ước lượng H 2×2, giải MRC/SFBC/MMSE, RS, CRC32, xuất PDU |
| [E3] Video PDU receiver | `ofdm321.blocks.VideoReceiver` | RS8+8 có kiểm chứng; ghi TS ra file và gửi UDP 5002 |
| [E4] 2×2 channel (disabled) | `ofdm321.blocks.MimoChannelSim` | Kênh mô phỏng |

Các khối [E] tìm gói `ofdm321` theo thứ tự: biến môi trường `OFDM_N321_DIR`, rồi thư mục cha của file sinh ra
(vì thế nên đặt `.grc` trong `OFDM_N321_MIMO/grc/`), rồi `~/Desktop/OFDM_N321_MIMO`.

Chạy mô phỏng không cần radio:

1. Disable 2 khối UHD, enable [E4].
2. Nối E1 → E4 → E2 và E4 → khối phổ; đặt `throttle=True` ở E1.
3. Đặt `ts_path` trỏ tới một file `.ts`.

Bản mô phỏng đã chạy thử với TS 640×360: nhận đủ 717/717 DATA, file thu giống hệt file phát, không mất khung.

Ghi chú về đồng bộ: trong GRC, cả hai khối UHD dùng `Sync = PC Clock`; với 2 kênh, gr-uhd tự hẹn giờ bắt đầu luồng.
Nếu còn lệch cố định ngắn hơn CP 16 mẫu thì ước lượng H từ LTF hấp thụ được. `run_n321.py` thì đặt
`set_time_now(0)` và `set_start_time(1 s)` một cách tường minh.
