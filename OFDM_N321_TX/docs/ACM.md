# Điều chế thích ứng (ACM), v0.3–v0.5

Từ v0.3, máy phát tự chọn điều chế cho **từng khung** (BPSK, QPSK, 16QAM hoặc 64QAM) dựa trên SNR mà máy thu
báo ngược về. Cách làm giống ACM của DVB-S2: kênh tốt thì tăng tốc độ, kênh xấu thì hạ xuống điều chế bền hơn
thay vì mất khung. Bật bằng `--acm` ở **cả hai** máy.

![ACM: 64QAM, 16QAM, QPSK khi SNR giảm](img/acm_gui.png)

## 1. Nguyên lý

```
TX: hàng đợi gói ─► chọn điều chế m (AcmController) ─► khung ACM: k(m) codeword ─► OFDM 2x2 ─► RF
                         ▲                                                             │
                         │ UDP 5003, 50 lần/s: {SNR_eff, tỉ lệ codeword lỗi, m đang thu}│
RX: ◄────────────────────┴──── FeedbackSender ◄── SNR_eff từng khung (sau cân bằng) ◄──┘
```

**Khung ACM có độ dài cố định:** 4 symbol mào đầu + 42 symbol dữ liệu + khe 80 mẫu = 3760 mẫu. Mỗi khung chứa
số codeword RS(248,216) tùy theo điều chế:

| Điều chế | Codeword/khung (siso, simo, alamouti) | smux | TS sau RS8+8 ở 1 MS/s (alamouti) | Ngưỡng SNR_eff, RS | Ngưỡng SNR_eff, LDPC (v0.6) |
|---|---|---|---|---|---|
| BPSK | 1 | 2 | 200 kb/s | 9 dB | 9 dB |
| QPSK | 2 | 4 | 400 kb/s | 14 dB | 12 dB |
| 16QAM | 4 | 8 | 800 kb/s | 20 dB | 19 dB |
| 64QAM | 6 | 12 | 1200 kb/s | 26 dB | 24 dB |

Với `--fec ldpc` (v0.6), mỗi gói trong khung ACM vẫn chiếm đúng chỗ của một codeword RS (2016 bit), nên số gói/khung
và dung lượng không đổi; mã là LDPC 802.11n tỉ lệ 5/6 rút ngắn + đục lỗ (xem [LDPC.md](LDPC.md)). Bảng ngưỡng riêng
cho LDPC đo bằng `tools/acm_calibrate.py` (`results/acm_thresholds.json`); BPSK là bậc thấp nhất nên ngưỡng của nó
không được dùng.

Vì độ dài khung không đổi, phần đồng bộ của máy thu giữ nguyên; máy thu chỉ đọc điều chế trong SIG rồi giải điều chế theo đó.

**SIG của khung ACM** chứa 24 bit (byte chế độ/điều chế có cờ ACM, 8 bit số thứ tự, CRC8). 24 bit này được phát
**hai lần** trên hai nửa dải, cách nhau 24 sóng mang con. Máy thu cộng mềm hai bản, được lợi khoảng 3 dB cộng phân tập
tần số, nên SIG không còn là khâu yếu nhất khi kênh xấu.

**SNR_eff** là SNR sau cân bằng, lấy trung bình điều hòa trên các sóng mang con và luồng, đúng đại lượng đã dùng
làm trọng số giải mềm. Nhờ vậy nó đã tính đến phân tập MIMO và fading chọn lọc tần số. Máy thu lọc thêm
(EWMA 0,8/0,2) trước khi gửi.

**Bộ điều khiển (máy phát):**

- **Hạ ngay** khi SNR_eff < ngưỡng + `--acm-margin` (mặc định 1 dB), hoặc khi tỉ lệ codeword lỗi > 10%. Tỉ lệ lỗi
  chỉ được tính sau khi máy thu đã báo là đang nhận đúng điều chế mới, vì các khung còn trên đường truyền là điều chế cũ.
- **Tăng từng bậc** khi SNR_eff ≥ ngưỡng bậc trên + margin + 1 dB (trễ), giữ liên tục 0,5 s.
- **Mất phản hồi quá 1,5 s** thì về điều chế bền nhất.
- Đo **độ trễ vòng** (từ lúc đổi điều chế đến khi máy thu xác nhận) và ghi vào log cùng `report.json`.

Ngưỡng ở bảng trên lấy từ `tools/acm_calibrate.py`: kênh 2×2 Rayleigh ngẫu nhiên có tiếng vọng, giải mềm bật.
Muốn hiệu chỉnh lại cho phần cứng thật thì sửa `THRESHOLDS_DB` trong `ofdm321/acm.py`.

## 2. Chạy mô phỏng (một PC, không cần radio)

```bash
# Terminal 1, folder RX:
bash scripts/sim_rx_acm.sh --gui          # SNR đổi theo bậc 30/22/14/11/20/28 dB, mỗi bậc 6 s
# Terminal 2, folder TX, sau khi RX in "RX listening":
bash scripts/sim_tx_acm.sh
```

- Log TX in điều chế đang dùng, SNR_eff máy thu báo về, số lần chuyển và độ trễ vòng.
- Log RX có thêm `ACM <điều chế> SNR_eff <dB> (sim SNR <dB>)`.
- Cửa sổ `--gui` cho thấy chòm sao đổi giữa 64QAM, 16QAM, QPSK và BPSK.

Các tùy chọn mô phỏng khác:

- `--snr-profile sine:8:30:40`: SNR dao động hình sin từ 8 đến 30 dB, chu kỳ 40 s.
- `--snr-profile ramp:30:6:60`: SNR giảm dần từ 30 xuống 6 dB trong 60 s.
- `--fb-delay 0.5`: thêm 0,5 s trễ cho kênh phản hồi, giả lập vệ tinh địa tĩnh (GEO).

Nếu độ trễ vòng trong log TX tăng dần lên hàng giây, máy không xử lý kịp thời gian thực (mẫu dồn ứ trong ZeroMQ).
Khi đó thêm `--rate 400e3` ở **cả hai** bên.

## 3. Chạy thật với hai N321

```bash
# Máy thu (folder RX). acm-host là IP của PC chạy máy phát (cùng PC thì bỏ qua):
bash scripts/start_rx.sh --acm --acm-host 192.168.1.20 --play --gui
# Máy phát (folder TX):
bash scripts/start_tx.sh --acm --source file --input ~/Videos/clip.mp4 --seconds 120
# Thử ACM bằng cách đổi gain phát theo thời gian (không bao giờ vượt --tx-gain):
bash scripts/start_tx.sh --acm --tx-gain 25 --tx-gain-profile steps:25,15,5,15,25:10
```

- PC phát phải mở cổng UDP 5003 (`sudo ufw allow 5003/udp` nếu có bật tường lửa). Đổi cổng bằng `--acm-port` ở cả hai bên.
- Hai bên vẫn phải cùng `--mimo`, `--freq`, `--rate`. `--mod` không còn tác dụng khi bật `--acm`.
- Chỉ dùng một số điều chế: `--acm-modcods qpsk,16qam,64qam` (bên TX).
- **Video:** từ v0.5, bitrate video đi theo điều chế (mục 3b). Bản cũ (bitrate cố định `ts_muxrate` 260 kb/s) vẫn
  dùng được bằng `--no-adaptive-video`.

### 3b. Bitrate video theo ACM (v0.5)

Kết quả RF 19/11 (SISO, 16QAM chiếm 89% khung) cho thấy với video cố định 260 kb/s, phần lớn khung 16QAM chỉ chở
IDLE. Từ v0.5, khi bật `--acm`, máy phát đổi bitrate video theo điều chế đang dùng (mặc định bật; tắt bằng `--no-adaptive-video`).

**Cách làm (giống HLS/DASH):** một tiến trình FFmpeg giải mã nguồn **một lần** rồi mã hóa song song mỗi điều chế
một bản MPEG-TS (bộ lọc `split` → N bộ libx264). Các bản có cùng khung hình, GOP 1 s, khung khóa ở cùng vị trí và
cùng `muxdelay`, nên PTS trùng nhau. Mỗi bản ra một pipe riêng. Máy phát chỉ gửi một bản và chỉ chuyển sang bản khác
**tại khung khóa** (GOP g của bản cũ nối với GOP g của bản mới). Continuity counter của TS được đánh số lại, còn PID,
PAT/PMT và SPS/PPS giống nhau ở mọi bản. Nhờ vậy `video_tx.ts` ghép lại và ffplay bên thu giải mã liền mạch
(0 cảnh báo giải mã trong các lượt mô phỏng).

Phía dưới tầng TS không có gì thay đổi: số thứ tự DATA liên tục, nhóm RS8+8 và END giữ nguyên, định dạng gói OVN1
giữ nguyên, máy thu kiểm chứng SHA-256 như trước.

| Điều chế | TS mux (75% dung lượng, 1 MS/s, siso/alamouti) | H.264 |
|---|---|---|
| BPSK | 150 kb/s | 115 kb/s |
| QPSK | 300 kb/s | 231 kb/s |
| 16QAM | 600 kb/s | 462 kb/s |
| 64QAM | 900 kb/s | 692 kb/s |

Bậc được tính từ `capacity()` theo `--rate` và `--mimo` (ví dụ smux gấp đôi, 400 kS/s nhân 0,4).

- **Hạ:** khi ACM hạ điều chế, video hạ theo ngay ở khung khóa kế tiếp (≤ 1 s).
- **Tăng:** chỉ khi điều chế cao hơn đã giữ ổn định ≥ 5 s (chống dao động).
- Chỉnh trong `config/default.json`: `adaptive_video_fill` (0,75), `adaptive_video_hold_s` (5).
- Log TX in thêm `VIDEO down/up: <điều chế> -> TS <kb/s>` và mỗi giây `video <điều chế> level: TS ..., backlog ... s`.
- `report.json` của TX có mục `video`: bậc bitrate (`ladder`), số lần đổi (`switches`), lịch sử (`history`: thời điểm,
  GOP, điều chế, TS và H.264 kb/s, lý do), backlog lớn nhất; `tx.tx_queue_max` là hàng đợi lớn nhất.

### 3c. Phát lại khi hạ điều chế, bộ đệm sắp xếp lại (v0.5)

Khi SNR tụt đột ngột, các khung đã phát trong khoảng một vòng phản hồi vẫn mang điều chế cũ và phần lớn bị hỏng.
Video càng lấp đầy khung thì càng mất nhiều gói (bản cố định 260 kb/s ít bị vì khung chủ yếu là IDLE).

- **Máy phát** (`--acm-resend`, mặc định 0,1 s; `0` = tắt): khi ACM hạ điều chế, các gói đã gửi ở điều chế cao hơn
  trong `max(0,1 s, 1,5 × độ trễ vòng)` vừa qua được phát lại trước hàng đợi.
- **Máy thu** (`--reorder`, mặc định 0,3 s khi có `--acm`; `0` = tắt): gói chờ tối đa 0,3 s trong một bộ đệm sắp
  theo (nhóm RS, DATA trước parity, chỉ số) rồi mới vào RS8+8. Ở mức SNR mới vẫn có vài codeword của điều chế cũ
  giải được và đến trước các gói phát lại; không có bộ đệm này, chúng sẽ đóng nhóm RS sớm. Video bên thu trễ thêm 0,3 s.
- Bản sao trùng bị máy thu bỏ qua (`duplicate_or_late` trong report), không đổi định dạng gói.
- Phản hồi UDP gửi 50 lần/s (trước là 10 lần/s): độ trễ vòng giảm từ 0,10 xuống 0,04 s.

### Không có mạng chung giữa hai PC?

Từ v0.4, phản hồi có thể đi luôn qua sóng radio, bằng kênh B (nhãn RF1 trên mặt máy) của hai N321. Cách này cần chạy `--mimo siso`.
Xem [PHAN_HOI_RADIO.md](PHAN_HOI_RADIO.md).

## 4. Kết quả mô phỏng

### v0.5: bitrate theo ACM (Rayleigh, CFO 2 kHz, SFO 5 ppm, SNR bậc 30/22/14/22/30 dB mỗi bậc 8 s, 1 MS/s, video 40 s)

| Cấu hình | RX | Gói DATA | TS video trung bình | Đổi bitrate | Hàng đợi TX max | Backlog video max | Độ trễ vòng | Phát lại |
|---|---|---|---|---|---|---|---|---|
| alamouti, UDP, bitrate theo ACM | `SIM_RX_VERIFIED` | 13960, thiếu 0 | 525 kb/s | 4 | 64/64 | 1,24 s | 0,04 s | 321 gói |
| alamouti, UDP, `--no-adaptive-video` | `SIM_RX_VERIFIED` | 6915, thiếu 0 | 260 kb/s | – | 64/64 | – | 0,04 s | 165 gói |
| siso, phản hồi radio, bitrate theo ACM | `SIM_RX_VERIFIED` | 8876, thiếu 0 | 334 kb/s | 4 | 64/64 | 0,41 s | 0,04 s | 107 gói |

Lịch sử bitrate (alamouti, UDP): 64QAM 900 kb/s → 16QAM 600 (giây 4,2) → BPSK 150 (12,8) → 16QAM 600 (25,6) →
64QAM 900 (32,6). Ở siso: 16QAM 600 → QPSK 300 → BPSK 150 → QPSK 300 → 16QAM 600. Video thu 600/600 khung,
SHA-256 trùng TX. Hàng đợi 64 gói chỉ đầy trong vài giây ngay sau mỗi lần hạ điều chế, rồi rút về 0.

Trước khi có 3c (cùng profile, UDP 10 lần/s): thiếu 80 gói DATA khi bitrate theo ACM, 24 gói khi bitrate cố định.
Phản hồi 50 lần/s còn 40 gói; thêm phát lại còn 24; thêm bộ đệm sắp xếp lại: 0.

Đồ thị: `python3 tools/plot_run.py <TX report> <RX report>` (xem mục 6).

### v0.3 (alamouti, Rayleigh, CFO 2 kHz, SFO 5 ppm, SNR 30/22/14/11/20/28 dB)

| Cấu hình | Gói DATA mất (trước RS8+8 bù) | Dung lượng trung bình |
|---|---|---|
| ACM, phản hồi tức thời (độ trễ vòng ≈ 0,1 s) | 80 / 6219 (1,3%), chỉ ở các lần SNR tụt đột ngột 8 dB | 3,4 codeword/khung (+71% so với QPSK cố định) |
| ACM, phản hồi trễ 0,5 s (giả lập GEO) | 371 / 6219 (6%) | tương tự |
| 16QAM cố định | 2936 / 6219 (47%), mất ở các bậc 14 và 11 dB | 4 codeword/khung khi còn sống |
| QPSK cố định | 0 | 2 codeword/khung |

Các lượt ACM chạy ở 400 kS/s vì máy kiểm thử chỉ có 2 nhân, không giữ được thời gian thực ở 1 MS/s với cả TX, RX
và bộ mô phỏng kênh. Các lượt cố định chạy ở 1 MS/s; tỉ lệ mất không phụ thuộc tốc độ lấy mẫu.

Nhận xét:

- ACM giữ được liên kết ở SNR thấp như QPSK và tận dụng kênh tốt gần bằng 64QAM.
- Gói mất chỉ xảy ra trong khoảng một vòng phản hồi sau mỗi lần SNR tụt mạnh. Trễ phản hồi càng lớn (vệ tinh) thì
  mất càng nhiều. Cách bù: tăng `--acm-margin` hoặc dựa thêm vào RS8+8 ngoài.
- Khi viết ACM, đã phát hiện và sửa một lỗi của bộ mô phỏng lệch clock: nội suy tuyến tính làm suy giảm các sóng
  mang con ở mép dải tới khoảng 11 dB. Nay đã thay bằng bộ nội suy windowed-sinc 16 tap.

## 5. File liên quan

| File | Nội dung |
|---|---|
| `ofdm321/video_adapt.py` | Bitrate theo ACM: bậc, `VideoRatePolicy`, `AdaptiveEncoder` (FFmpeg nhiều bản, ghép tại khung khóa) |
| `ofdm321/media.py` | `ladder_command` (một FFmpeg, N bản TS) |
| `ofdm321/acm.py` | `AcmController`, `FeedbackSender` (RX), `FeedbackReceiver` (TX), bảng ngưỡng |
| `ofdm321/modem.py` | Khung ACM (`NP_ACM`, `codewords_per_frame`), SIG lặp, BPSK/64QAM, SNR_eff |
| `ofdm321/blocks.py` | `MimoTxSource(acm=...)`, `MimoRxSink` đếm theo codeword, `parse_snr_profile` |
| `tools/acm_calibrate.py` | Đo CER theo SNR_eff để đặt ngưỡng |
| `ofdm321/blocks.py` (v0.5) | phát lại khi hạ điều chế (`MimoTxSource`), bộ đệm sắp xếp lại (`VideoReceiver`) |
| `tools/plot_run.py` | Vẽ SNR, điều chế, bitrate, mất gói theo thời gian từ `report.json` |
| `tests/test_modem.py` | `test_acm_frames_any_modulation`, `test_acm_controller`, `test_video_rate_policy`, `test_ts_splice_helpers`, `test_adaptive_encoder_splices_cleanly`, `test_acm_resend_on_downshift`, `test_rx_reorder_buffer` |

## 6. Lịch sử theo thời gian và đồ thị (v0.5)

`report.json` của cả hai bên có mục `history`, mỗi giây một dòng:

- **RX:** `t`, `t_wall`, `ok`, `rs`, `sig`, `crc`, `lost` (bộ đếm cộng dồn), `snr_db`, `evm_db`, `snr_eff_db`,
  `modulation`, `rx_rms`, `cond_db`, `data`, `missing`.
- **TX:** điều chế, SNR_eff nhận được, hàng đợi, gói phát lại, bitrate TS và backlog video, và khi dùng phản hồi radio:
  số khung phản hồi mỗi giây và SNR đường phản hồi.

```bash
python3 tools/plot_run.py runs/<lượt RX>                         # chỉ RX
python3 tools/plot_run.py <report.json TX> <report.json RX>      # cả hai (chép report của máy kia sang)
python3 tools/plot_run.py --latest                               # lượt mới nhất của folder này
```

Ảnh `plot_run.png` được lưu vào thư mục của từng lượt chạy, gồm 4 khung: SNR/SNR_eff, điều chế (dạng bậc),
bitrate video so với dung lượng (kèm hàng đợi TX), lỗi/mất gói/phát lại mỗi giây. TX và RX được ghép theo
đồng hồ máy, nên khi chạy trên hai PC, độ khớp phụ thuộc đồng hồ hai máy.

## 7. Độ trễ vòng ACM và `--tx-lead-ms` (v0.6.1)

Lượt RF 23/11 (v0.6.0, siso + phản hồi radio) có độ trễ vòng trung vị 0,398 s, tính từ lúc máy phát đổi điều chế
tới lúc báo cáo đầu tiên về điều chế mới quay lại máy phát. Mô phỏng cũ chỉ cho 0,04–0,09 s. Nguyên nhân: cả
`MimoTxSource` (video, máy phát) và `FeedbackRadioSource` (phản hồi, máy thu) đều sinh mẫu nhanh hết mức các bộ đệm
GNU Radio + UHD + USRP cho phép. Mẫu vừa tạo phải xếp hàng sau cả bộ đệm, nên một khung (hay một báo cáo SNR) chỉ
lên sóng sau một thời gian bằng dung lượng bộ đệm chia cho tốc độ mẫu. Với khoảng 131 nghìn mẫu mỗi luồng, phần này là
0,13 s ở 1 MS/s (video) cộng 0,26 s ở 500 kS/s (phản hồi), khoảng 0,39 s. Đây là giả thuyết khớp với số đo RF; lượt RF
tới sẽ đo trực tiếp.

**Đo từng đoạn** (ghi vào `report.json` → `latency`, và mỗi giây trong `history`):

| Đoạn | Ở đâu | Cách đo |
|---|---|---|
| Bộ đệm TX video (`tx_video.buffer`, `tx_buffer_ms`) | máy phát | thời điểm lên sóng của mẫu đầu khung − lúc tạo khung. Thời điểm lên sóng = mốc start của USRP (timed start, `get_time_now()` chỉnh lại mỗi giây) + chỉ số mẫu / Fs |
| Đổi điều chế → lên sóng (`acm_switch_to_air`) | máy phát | quyết định của bộ điều khiển → khung đầu tiên của điều chế mới lên sóng |
| Xử lý RX (`rx.proc`, `rx_proc_ms`) | máy thu | mẫu cuối khung vào bộ đệm vòng 2^18 → giải xong (lô tới 16 khung) |
| Anten → bộ đệm RX (`rx.ingest`, chỉ RF) | máy thu | lúc mẫu vào `MimoRxSink` − thời điểm lấy mẫu theo đồng hồ USRP |
| Bộ đệm TX phản hồi (`feedback_tx.buffer`, `fb_tx_buffer_ms`) | máy thu | lúc báo cáo lên sóng − lúc tạo báo cáo |
| Xử lý phía nhận phản hồi (`feedback_rx.proc`) | máy phát | như xử lý RX, ở 500 kS/s |
| Cả vòng (`acm_loop`) | máy phát | đổi điều chế → báo cáo đầu tiên với điều chế mới |

**Sửa:** hai nguồn phát không sinh mẫu sớm hơn thời điểm lên sóng quá `--tx-lead-ms` (mặc định 30 ms; đặt cho cả hai
bên vì máy thu cũng phát phản hồi). Bộ đệm đầu ra GNU Radio của nguồn được giới hạn ở `tx_buffer_samples` (8192).
`--tx-lead-ms 0` quay về cách cũ.

**Mô phỏng có cùng cơ chế:** `TxDeviceSim` phát mẫu đúng tốc độ thực, và trước nó là một bộ đệm GNU Radio cỡ bộ đệm USRP
(`--sim-dev-buffer`, mặc định 131072 mẫu mỗi luồng). Kết quả, siso + phản hồi radio, SNR theo bậc 30/22/14/22/30 dB:

| | Vòng ACM | Bộ đệm TX video | Đổi → lên sóng | Xử lý RX | Bộ đệm TX phản hồi | Xử lý nhận phản hồi |
|---|---|---|---|---|---|---|
| `--tx-lead-ms 0` (như v0.6.0) | 428 ms | 135 ms | 137 ms | 3,5 ms | 267 ms | 3,0 ms |
| `--tx-lead-ms 30` (mặc định) | **78 ms** | 25 ms | 29 ms | 3,4 ms | 24 ms | 2,9 ms |

Phần còn lại của vòng là thời lượng một khung phản hồi (7,5 ms), chờ khung phản hồi kế tiếp, và lọc EWMA của máy thu.
Nếu log của UHD in `U` (underflow) hoặc `tx_late_frames` tăng, nghĩa là Python không kịp đáp ứng mức lead đặt ra; khi
đó tăng `--tx-lead-ms` lên 50–80.

## 8. Chống nhảy điều chế (v0.6.1)

Lượt RF 23/11: ở SNR_eff trung vị 18,5 dB (QPSK cần ≥ 15 dB), ACM nhảy BPSK↔QPSK 9 lần trong 15 s. Mỗi lần hạ đều do
**một** báo cáo SNR đơn lẻ khoảng 14,3 dB, nhiều lần ngay sau khi vừa tăng. Bộ điều khiển v0.6.0 hạ ngay ở báo cáo đầu
tiên dưới ngưỡng. Quy tắc mới (máy phát, `acm_debounce`, mặc định bật):

- **Hạ theo SNR** chỉ khi ≥ 3 báo cáo liên tiếp dưới ngưỡng (`acm_down_reports`), kéo dài ≥ 30 ms (`acm_down_hold_s`).
  Chỉ tính báo cáo có `rx_mod` trùng điều chế hiện tại; báo cáo về khung còn mang điều chế cũ không dùng cho quyết
  định theo SNR.
- **Hạ khẩn, ngay lập tức:** tỉ lệ codeword lỗi > 10% (của điều chế hiện tại), hoặc SNR thấp hơn cả ngưỡng của bậc
  dưới. Trường hợp thứ hai áp dụng với mọi báo cáo; khi đó hạ thẳng xuống bậc cao nhất còn đủ SNR.
- **Sau khi tăng:** chờ máy thu xác nhận điều chế mới, rồi thêm 0,3 s (`acm_up_guard_s`), mới cho phép hạ theo SNR.
  CER > 10% vẫn hạ ngay.
- Tăng giữ như cũ: SNR ≥ ngưỡng bậc trên + biên + trễ 1 dB, liên tục 0,5 s, cũng chỉ dựa trên báo cáo của điều chế hiện tại.
- `--no-acm-debounce` (máy phát) quay về v0.6.0. `report.json` → `acm.ignored_dips` đếm số lần sụt ngắn đã bỏ qua.

Test `test_acm_no_chatter_at_18_5_db` phát lại chuỗi 18,5 dB có các mẫu 14,3 dB (báo cáo mỗi 8 ms, vòng 80 ms): v0.6.0
chuyển 41 lần trong 15 s, bản mới 0 lần. Một lần sụt thật xuống 13 dB vẫn được hạ trong ≤ 50 ms.

Mô phỏng hai tiến trình (siso + phản hồi radio, `--tx-lead-ms 30`):

| `--snr-profile` | Số lần chuyển v0.6.0 / v0.6.1 | Gói video mất v0.6.0 / v0.6.1 | Ghi chú |
|---|---|---|---|
| `dips:19:9:0.8:15` (19 dB, sụt 9 dB trong 15 ms, 0,8 lần/s) | **33 / 1** | 0 / 0 | bản mới gửi 7987 gói DATA so với 4672 (bitrate video giữ ở mức QPSK) |
| `steps:30,22,14,22,30:8` | 8 / 8 | 0 / 0 | |
| `sine:12:28:30` | 8 / 8 | 0 / 0 | 17 lần sụt ngắn được bỏ qua |
| `sine:6:30:40` | 8 / 8 | 848 / 840 | mất gói khi SNR xuống 6 dB (giới hạn vật lý của siso) |

Profile `dips:BASE:DEPTH:RATE:MS` (mới) tạo các lần fade ngắn ngẫu nhiên để tái hiện hiện tượng RF trong mô phỏng.
