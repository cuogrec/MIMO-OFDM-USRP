# Phản hồi ACM qua sóng radio (v0.4–v0.5)

Bên thu gửi SNR về bên phát **qua chính hai N321**, không cần Wi-Fi hay mạng giữa hai PC. Đường video chạy SISO
trên kênh A (nhãn **RF0** trên mặt máy); đường phản hồi chạy ngược chiều trên kênh B (nhãn **RF1**), ở một tần số
khác. Tổng cộng vẫn chỉ cần 4 anten.

```
N321 #1 (PC phát)                                      N321 #2 (PC thu)
  RF0 TX/RX ── video OFDM ACM, 921 MHz ─────────────────────►  RF0 RX2
  RF1 RX2   ◄── phản hồi SISO BPSK, 922,7 MHz, ~133 khung/s ──  RF1 TX/RX
```

## 1. Bố trí anten

| Máy | Kênh A (RF0) | Kênh B (RF1) |
|---|---|---|
| N321 #1 (`.3`, PC phát) | **RF0 TX/RX**: phát video | **RF1 RX2**: thu phản hồi |
| N321 #2 (`.95`, PC thu) | **RF0 RX2**: thu video | **RF1 TX/RX**: phát phản hồi |

Từ v0.5, lúc khởi động cả hai chương trình in một dòng `Cắm anten (...)` liệt kê đúng các cổng trên cho chế độ đang
chạy. Trong `report.json`, mục `hardware` ghi cổng của từng luồng (`video_tx_RF0`, `feedback_rx_RF1`, ...).

Trên mỗi máy, đặt anten kênh B cách anten kênh A càng xa càng tốt (≥ 30 cm) để giảm tự nhiễu: máy vừa phát vừa thu
ở hai tần số cách nhau khoảng 1,5 MHz.

## 2. Chạy

```bash
# PC thu (N321 .95), chạy trước:
cd ~/MIMO-OFDM-USRP/OFDM_N321_RX
bash scripts/start_rx.sh --mimo siso --acm --fb-link radio --play --gui --freq 921e6 --rx-gain 50

# PC phát (N321 .3):
cd ~/MIMO-OFDM-USRP/OFDM_N321_TX
bash scripts/start_tx.sh --mimo siso --acm --fb-link radio --freq 921e6 --tx-gain 30 \
     --acm-modcods qpsk,16qam,64qam --source file --input ~/Videos/clip.mp4 --seconds 60
# Thử ACM bằng gain phát thay đổi theo thời gian (không vượt --tx-gain):
#    thêm --tx-gain-profile steps:30,20,10,20,30:10
```

- `--fb-link radio` bắt buộc đi cùng `--acm` và `--mimo siso` ở **cả hai** bên.
- Tần số phản hồi mặc định 922,7 MHz (`--fb-freq`), tốc độ lấy mẫu 500 kS/s (`--fb-rate`), băng chiếm khoảng
  0,4 MHz (922,5–922,9 MHz), vẫn trong dải 920–923 MHz. Hai bên phải cùng `--fb-freq` và `--fb-rate`.
- **Gain phản hồi đã chốt sau lượt RF 19/11:** `--fb-tx-gain 30` ở bên thu (mức tối đa khi dùng anten) và
  `--fb-rx-gain 60` ở bên phát (mức tối đa của N321). Đây là giá trị mặc định nên không cần gõ. Phản hồi yếu thì
  không còn gain để tăng: đưa anten RF1 của hai máy lại gần hoặc dùng `--fb-rate 250e3` (cả hai bên).
- Từ v0.5, bitrate video đi theo điều chế (xem [ACM.md](ACM.md), mục 3b), nên dung lượng 16QAM/64QAM được dùng cho video.

## 3. Đọc log

Bên phát in thêm mỗi giây:

```
   radio feedback: 4526 frames (4104 used), link SNR 22.4 dB, lost 0
```

| Trường | Nên thấy | Nếu không |
|---|---|---|
| `frames` | Tăng khoảng 133 khung/s (66 khung/s nếu `--fb-rate 250e3`) | Đứng ở 0: kiểm tra anten RF1 RX2 (máy phát) và RF1 TX/RX (máy thu); gain đã ở mức tối đa (30/60) nên đưa hai anten RF1 lại gần hoặc thử `--fb-rate 250e3` |
| `used` | Tăng gần bằng `frames` khi video đã chạy | Phản hồi chỉ được dùng khi bên thu vừa giải được khung ACM (trong 0,5 s) |
| `link SNR` | Trên khoảng 10 dB | BPSK chịu được SNR thấp, nhưng nên giữ trên 10 dB |
| `loop` (dòng TX data) | Khoảng 0,05–0,1 s | Lớn hơn nhiều: phản hồi bị mất, hoặc máy không kịp thời gian thực |

Bên thu in thêm `fb <số> frames`: số khung phản hồi đã phát.

**Kiểm tra lúc khởi động (v0.5):** nếu 5 s sau khi máy phát chạy mà chưa nhận được khung phản hồi nào, log TX in

```
WARNING Chưa nhận phản hồi: kiểm tra anten RF1 RX2 (máy phát) và RF1 TX/RX (máy thu), tăng --fb-rx-gain/--fb-tx-gain (5 s, 0 khung phản hồi)
```

rồi lặp lại mỗi 10 s cho đến khi nhận được. Khi có khung đầu tiên, log in `Đã nhận phản hồi radio sau X s`.
Thường gặp nhất là máy thu chưa chạy (phải chạy trước), sai `--fb-freq`/`--fb-rate` giữa hai bên, hoặc anten cắm nhầm cổng.

`report.json` của TX ghi `acm.radio_feedback`: `first_frame_s`, `no_feedback_warnings`, `link_snr_db` (min/trung vị/max),
và mỗi giây trong `history` có `fb_frames_per_s`, `fb_snr_db`. Đồ thị: `python3 tools/plot_run.py <report TX> <report RX>`.

**Tự nhiễu:** nếu bật phản hồi làm video ở bên thu xấu đi (EVM tăng, `rs` tăng), giảm `--fb-tx-gain`
(mặc định 30 là cao nhất, thử 15–20), đưa anten RF1 TX/RX ra xa anten RF0 RX2, hoặc kéo xa hai tần số (ví dụ `--freq 920.5e6 --fb-freq 922.7e6`).

## 4. Kết quả RF (19/11/2026)

Hai N321 dùng anten, SISO, 921 MHz, ACM với phản hồi qua radio (922,7 MHz), `fb_tx_gain 30` / `fb_rx_gain 60`:

| Chỉ số | Giá trị |
|---|---|
| Kết quả | `RF_RX_VERIFIED`, 0 gói mất |
| SNR / EVM | 24 dB / −20 dB |
| Điều chế | 16QAM chiếm 89% khung |
| Hạn chế đã thấy | Video cố định 260 kb/s: phần lớn khung 16QAM chỉ chở IDLE (dư dung lượng). v0.5 sửa bằng bitrate theo ACM |

## 5. Mô phỏng (một PC, không cần radio)

```bash
# Terminal 1, folder RX:
bash scripts/sim_rx_fbradio.sh --gui
# Terminal 2, folder TX:
bash scripts/sim_tx_fbradio.sh
```

Đường phản hồi trong mô phỏng đi qua ZeroMQ `tcp://127.0.0.1:5561` (ngược chiều đường video 5560). Trên đường đó
có thêm nhiễu (`--fb-snr`, mặc định 20 dB) và lệch tần số 1,5 kHz, giống một sóng mang thật.

Kết quả mô phỏng (SISO, Rayleigh, CFO 2 kHz, SFO 5 ppm, SNR đổi bậc 30/22/14/11/20/28 dB, 400 kS/s, phản hồi 250 kS/s):

| Chỉ số | Giá trị |
|---|---|
| Khung phản hồi nhận / dùng | 4805 / 4383, mất đồng bộ 0 |
| SNR đường phản hồi | 20–24 dB |
| Độ trễ vòng ACM | 0,04–0,09 s (UDP: khoảng 0,1 s) |
| Video | `SIM_RX_VERIFIED`, 6220/6220 gói DATA, thiếu 0 |
| Điều chế đã dùng (số khung) | BPSK 2786, QPSK 2567, 16QAM 1701 |

v0.5, cùng kênh nhưng SNR bậc 30/22/14/22/30 dB (mỗi bậc 8 s), 1 MS/s / 500 kS/s, bitrate video theo ACM:
`SIM_RX_VERIFIED`, 8876/8876 gói DATA, thiếu 0; bitrate đổi 4 lần (16QAM 600 → QPSK 300 → BPSK 150 → QPSK 300 →
16QAM 600 kb/s); TS trung bình 334 kb/s; hàng đợi TX lớn nhất 64/64 (vài giây sau mỗi lần hạ); độ trễ vòng 0,044 s;
khung phản hồi đầu tiên đến sau 1,0 s; SNR đường phản hồi 20–24 dB.

64QAM không được chọn: ở SNR mô phỏng 28–30 dB, SISO trên Rayleigh chỉ cho SNR hiệu dụng khoảng 26–27 dB, thấp hơn
ngưỡng 64QAM (26 dB + biên 1 dB + trễ 1 dB). Đây là hành vi đúng: SISO không có lợi phân tập như Alamouti.

## 6. Cách hoạt động

- **Bên thu** (`FeedbackRadioSource`): phát liên tục các khung OFDM SISO BPSK (3760 mẫu, 7,5 ms ở 500 kS/s).
  Mỗi khung chở một gói 212 byte: `OFB1`, session, số thứ tự, SNR hiệu dụng, tỉ lệ codeword lỗi, tuổi của số đo,
  điều chế đang thu, cờ hợp lệ. Gói có CRC32 + RS(248,216) như dữ liệu video, nên rất khó nhận sai.
- **Bên phát**: một `MimoRxSink` thứ hai (SISO BPSK ở 500 kS/s) dò và giải khung trên RF1 RX2, rồi đưa báo cáo vào
  chính `AcmController` của bản UDP. Bộ điều khiển không đổi; chỉ đường chở báo cáo đổi.
- **N321:** mỗi máy mở hai streamer trên cùng thiết bị: một hướng trên RF0 (kênh A, video), hướng ngược lại trên RF1 (kênh B,
  phản hồi), mỗi hướng tốc độ lấy mẫu và tần số riêng. Lúc khởi động, chương trình kiểm tra cả hai kênh đã khóa
  đúng tần số (LO locked); nếu không sẽ báo lỗi và dừng.

File liên quan: `ofdm321/acm.py` (`FeedbackReporter`, `RadioFeedbackHandler`, định dạng gói),
`ofdm321/blocks.py` (`FeedbackRadioSource`), `ofdm321/hardware.py` (`make_stream`, `configure_streams`),
`ofdm321/runner_split.py` (`--fb-link radio`, kiểm tra phản hồi lúc khởi động), `ofdm321/acm.py` (`FeedbackWatchdog`),
`tests/test_modem.py` (`test_radio_feedback_packet`, `test_feedback_watchdog`).
