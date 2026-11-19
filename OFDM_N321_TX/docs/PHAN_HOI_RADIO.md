# Phản hồi ACM qua sóng radio (v0.4)

Bên thu gửi SNR về bên phát **qua chính hai N321**, không cần Wi-Fi hay mạng giữa hai PC. Đường video chạy SISO
trên kênh A (nhãn **RF0** trên mặt máy); đường phản hồi chạy ngược chiều trên kênh B (nhãn **RF1**), ở một tần số
khác. Tổng cộng vẫn chỉ cần 4 anten.

```
N321 #1 (PC phát)                                      N321 #2 (PC thu)
  TX/RX A ── video OFDM ACM, 921 MHz ───────────────────►  RX2 A
  RX2 B   ◄── phản hồi SISO BPSK, 922,7 MHz, ~133 khung/s ── TX/RX B
```

## 1. Bố trí anten

| Máy | Kênh A (RF0) | Kênh B (RF1) |
|---|---|---|
| N321 #1 (`.3`, PC phát) | **TX/RX A**: phát video | **RX2 B**: thu phản hồi |
| N321 #2 (`.95`, PC thu) | **RX2 A**: thu video | **TX/RX B**: phát phản hồi |

Trên mỗi máy, đặt anten kênh B cách anten kênh A càng xa càng tốt (≥ 30 cm) để giảm tự nhiễu: máy vừa phát vừa thu
ở hai tần số cách nhau khoảng 1,5 MHz.

## 2. Chạy

```bash
# PC thu (N321 .95), chạy trước:
cd ~/MIMO-OFDM-USRP/OFDM_N321_RX
bash scripts/start_rx.sh --mimo siso --acm --fb-link radio --play --gui --freq 921e6 --rx-gain 50 --fb-tx-gain 30

# PC phát (N321 .3):
cd ~/MIMO-OFDM-USRP/OFDM_N321_TX
bash scripts/start_tx.sh --mimo siso --acm --fb-link radio --freq 921e6 --tx-gain 30 --fb-rx-gain 60 \
     --acm-modcods qpsk,16qam,64qam --tx-gain-profile steps:30,20,10,20,30:10 \
     --source file --input ~/Videos/clip.mp4 --seconds 60
```

- `--fb-link radio` bắt buộc đi cùng `--acm` và `--mimo siso` ở **cả hai** bên.
- Tần số phản hồi mặc định 922,7 MHz (`--fb-freq`), tốc độ lấy mẫu 500 kS/s (`--fb-rate`), băng chiếm khoảng
  0,4 MHz (922,5–922,9 MHz), vẫn trong dải 920–923 MHz. Hai bên phải cùng `--fb-freq` và `--fb-rate`.
- Gain phản hồi: `--fb-tx-gain` ở bên thu (mặc định 30 = mức tối đa khi dùng anten), `--fb-rx-gain` ở bên phát
  (mặc định 60 = mức tối đa của N321). Phản hồi yếu thì không còn gain để tăng: đưa anten RF1 của hai máy lại gần
  hoặc dùng `--fb-rate 250e3`.

## 3. Đọc log

Bên phát in thêm mỗi giây:

```
   radio feedback: 4526 frames (4104 used), link SNR 22.4 dB, lost 0
```

| Trường | Nên thấy | Nếu không |
|---|---|---|
| `frames` | Tăng khoảng 133 khung/s (66 khung/s nếu `--fb-rate 250e3`) | Đứng ở 0: kiểm tra anten RX2 B (máy phát) và TX/RX B (máy thu); gain đã ở mức tối đa (30/60) nên đưa hai anten RF1 lại gần hoặc thử `--fb-rate 250e3` |
| `used` | Tăng gần bằng `frames` khi video đã chạy | Phản hồi chỉ được dùng khi bên thu vừa giải được khung ACM (trong 0,5 s) |
| `link SNR` | Trên khoảng 10 dB | BPSK chịu được SNR thấp, nhưng nên giữ trên 10 dB |
| `loop` (dòng TX data) | Khoảng 0,05–0,1 s | Lớn hơn nhiều: phản hồi bị mất, hoặc máy không kịp thời gian thực |

Bên thu in thêm `fb <số> frames`: số khung phản hồi đã phát.

**Tự nhiễu:** nếu bật phản hồi làm video ở bên thu xấu đi (EVM tăng, `rs` tăng), giảm `--fb-tx-gain`
(mặc định 30 là cao nhất, thử 15–20), đưa anten TX/RX B ra xa anten RX2 A, hoặc kéo xa hai tần số (ví dụ `--freq 920.5e6 --fb-freq 922.7e6`).

## 4. Mô phỏng (một PC, không cần radio)

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

64QAM không được chọn: ở SNR mô phỏng 28–30 dB, SISO trên Rayleigh chỉ cho SNR hiệu dụng khoảng 26–27 dB, thấp hơn
ngưỡng 64QAM (26 dB + biên 1 dB + trễ 1 dB). Đây là hành vi đúng: SISO không có lợi phân tập như Alamouti.

## 5. Cách hoạt động

- **Bên thu** (`FeedbackRadioSource`): phát liên tục các khung OFDM SISO BPSK (3760 mẫu, 7,5 ms ở 500 kS/s).
  Mỗi khung chở một gói 212 byte: `OFB1`, session, số thứ tự, SNR hiệu dụng, tỉ lệ codeword lỗi, tuổi của số đo,
  điều chế đang thu, cờ hợp lệ. Gói có CRC32 + RS(248,216) như dữ liệu video, nên rất khó nhận sai.
- **Bên phát**: một `MimoRxSink` thứ hai (SISO BPSK ở 500 kS/s) dò và giải khung trên RX2 B, rồi đưa báo cáo vào
  chính `AcmController` của bản UDP. Bộ điều khiển không đổi; chỉ đường chở báo cáo đổi.
- **N321:** mỗi máy mở hai streamer trên cùng thiết bị: một hướng trên kênh A (video), hướng ngược lại trên kênh B
  (phản hồi), mỗi hướng tốc độ lấy mẫu và tần số riêng. Lúc khởi động, chương trình kiểm tra cả hai kênh đã khóa
  đúng tần số (LO locked); nếu không sẽ báo lỗi và dừng.

File liên quan: `ofdm321/acm.py` (`FeedbackReporter`, `RadioFeedbackHandler`, định dạng gói),
`ofdm321/blocks.py` (`FeedbackRadioSource`), `ofdm321/hardware.py` (`make_stream`, `configure_streams`),
`ofdm321/runner_split.py` (`--fb-link radio`), `tests/test_modem.py` (`test_radio_feedback_packet`).
