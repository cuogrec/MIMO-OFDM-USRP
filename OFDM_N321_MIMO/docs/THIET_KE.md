# Thiết kế: truyền video OFDM 2×2 MIMO trên USRP N321

Project này làm lại hệ thống `OFDM_Video_N210` (v1.23) trên **một USRP N321** nối với máy
Ubuntu qua **10 GbE (SFP+)**. Mạch vòng chạy trên chính một máy N321, dùng cả hai kênh phát
và hai kênh thu, ở băng **915 MHz** (có thể đổi tần số).

## 1. Giữ gì, đổi gì so với bản N210

| Tầng | N210 v1.23 | N321 (project này) |
|---|---|---|
| Video | FFmpeg H.264 → MPEG-TS, gói 188 B | giữ nguyên (`media.py`) |
| Đóng gói ứng dụng | header 24 B `OVN1`, DATA / RS8_PARITY / END / IDLE | **giữ nguyên byte** (`protocol.py`) |
| FEC ngoài | RS8+8, chỉ giao DATA khi có ≥ k+2 shard và parity dư khớp | giữ nguyên (`fec_rs.py`, `verified_fec.py`) |
| FEC trong | CRC32 + RS(248,216) | giữ nguyên (`inner_rs.py`), CRC32 bằng `zlib` |
| PHY | `digital.ofdm_tx/rx` của GNU Radio, chỉ SISO | **modem MIMO mới viết bằng numpy** (`modem.py`) |
| RF | N210 + BasicTX/RX, real-IF 27,12 MHz, 1 kênh | N321, 2 TX + 2 RX, 915 MHz, LO thật |
| Đồng bộ | No Sync, một kênh | cùng một thiết bị, đặt chung mốc thời gian, hai luồng bắt đầu cùng lúc |

Không dùng `digital.ofdm_tx/rx`, vì hai khối này không hỗ trợ nhiều anten. Modem mới vẫn giữ FFT 64, CP 16,
48 sóng mang data và 4 pilot như bản cũ, nên các con số dễ so sánh.

## 2. Phần cứng N321 (thông số dùng trong thiết kế)

- Dải tần 3 MHz – 6 GHz, mỗi kênh rộng tối đa 200 MHz. **Dưới 450 MHz** máy thêm một tầng trộn, công suất
  TX tối đa chỉ khoảng +10 dBm và không đảm bảo đồng pha giữa các kênh. Vì vậy chọn 915 MHz thay vì 433 MHz.
- Master clock 200 / 245,76 / 250 MHz. Sample rate = MCR / N, với N nguyên từ 1 đến 1024, nên chọn N chẵn.
  Mặc định 200 MHz / 200 = **1 MS/s**.
- Công suất TX tối đa ở 450 MHz – 1 GHz: +20 dBm. Công suất vào RX khuyến nghị: P1dB ≈ **−15 dBm**.
  Gain TX và RX đều 0–60 dB.
- Cổng của mỗi kênh: `TX/RX` và `RX2`. Subdev cho hai kênh: `"A:0 B:0"`. Thiết kế phát trên `TX/RX`,
  thu trên `RX2` của cả hai kênh.
- Image FPGA: **HG** (mặc định) gồm SFP0 = 1 GbE, SFP1 = 10 GbE; **XG** cho cả hai cổng 10 GbE.
  IP mặc định: SFP0 192.168.10.2, SFP1 **192.168.20.2**, MTU 9000.
- N321 có thể xuất LO cho máy khác (tối đa 4 lần) để làm MIMO lớn. Project này không cần, vì chỉ có
  một máy và kênh truyền được ước lượng lại ở từng khung.
- Host phải có UHD ≥ 3.14. Filesystem (MPM) và FPGA trên N321 phải **cùng phiên bản UHD** với máy
  Ubuntu (UHD 4.6 trên Ubuntu 24.04).

## 3. Kết nối 10 GbE

Băng thông cần dùng = 2 kênh × Fs × 4 byte (sc16) cho mỗi chiều. Ở 1 MS/s là 8 MB/s mỗi chiều,
nên 10 GbE dư rất nhiều; ngay cả 1 GbE cũng đủ. Muốn tăng Fs lên vài chục MS/s thì 10 GbE mới phát
huy, khi đó giới hạn sẽ nằm ở CPU Python chứ không ở đường truyền.

Cấu hình máy Ubuntu (`scripts/setup_network_10g.sh`):

- Đặt card SFP+ IP 192.168.20.1/24, **MTU 9000**. MTU host và N321 lệch nhau sẽ gây lỗi streaming.
- `net.core.rmem_max = wmem_max = 33554432` (32 MB), ring buffer `ethtool -G rx 4096 tx 4096`.
- Kiểm tra bằng `tools/probe_n321.py --stream`. Công cụ này không phát RF: nó chạy uhd_usrp_probe,
  rồi benchmark RX hai kênh để đếm overrun.

## 4. Kiến trúc phần mềm

```
FFmpeg ─TS─► Encoder thread ─DATA/RS8_PARITY/END─► MimoTxSource ══2 luồng══► UHD Sink (A:0 TX/RX, B:0 TX/RX)
                                                     (modem.Modulator)              │ RF / cáp / anten
VideoReceiver ◄─PDU (đã qua CRC)─ MimoRxSink ◄══2 luồng══ UHD Source (A:0 RX2, B:0 RX2)
 (RS8+8 có kiểm chứng)             (modem.Demodulator)
   ├─► video_rx.ts
   └─► UDP 127.0.0.1:5002 ─► ffplay
```

- `MimoTxSource` là sync_block có 2 đầu ra. Khi có gói thì điều chế khung; khi hàng đợi rỗng thì phát
  khung IDLE dựng sẵn từ trước, để luồng RF chạy liên tục mà gần như không tốn CPU.
- `MimoRxSink` có 2 đầu vào, dùng ring buffer. Khi chưa khóa, nó tìm khung bằng Schmidl-Cox trên cả bộ đệm.
  Khi đã khóa, nó chỉnh lại thời điểm từng khung trong cửa sổ nhỏ, rồi giải điều chế **theo lô tới 16 khung**
  bằng numpy để kịp thời gian thực.
- Chế độ `sim` thay UHD bằng `MimoChannelSim`: kênh 2×2 phẳng hoặc 2 tap, cộng AWGN và CFO.

## 5. Khung OFDM

| Phần | Số symbol | Nội dung |
|---|---|---|
| STF | 1 | Chỉ dùng sóng mang chẵn, nên tín hiệu tuần hoàn chu kỳ 32 mẫu (Schmidl-Cox). TX1 phát bản dịch vòng 4 mẫu (CSD) để hai anten không triệt tiêu nhau |
| LTF | 2 | Ma trận P: TX0 phát [L, L], TX1 phát [L, −L]. Từ đó có H(k) cho từng cặp TX–RX và từng sóng mang; nhiễu đo trên các bin rỗng |
| SIG | 1 | 48 bit BPSK: magic 0xA5, mode, điều chế, số thứ tự khung, CRC16. Khi có 2 TX thì mã SFBC. Khung có SIG sai được giải thử với cấu hình đã biết (v0.6, mục 5.1); không qua RS + CRC32 thì bị bỏ |
| DATA | Np | Một từ mã 248 B (212 B ứng dụng + CRC32 + RS 32 B), đã scramble; 4 pilot mỗi symbol. Với `--fec ldpc` (v0.6): 2 codeword LDPC 802.11n mỗi gói, interleaver bit, xem `OFDM_N321_RX/docs/LDPC.md` |
| gap | 80 mẫu | Toàn số 0 |

Mỗi symbol gồm 16 mẫu CP + 64 mẫu FFT. Pilot của hai anten đều đã biết trước: bộ thu dự đoán pilot qua H
rồi bù pha chung (CPE) cho từng symbol. Cách này xử lý cả CFO dư lẫn trôi pha.

### 5.1 Dò khung, định thời và SIG ở SNR thấp (v0.6)

Đo bằng `tools/link_budget.py` (kết quả trong `results/stage_breakdown.*`): ở v0.5, ngưỡng FER 1% của BPSK
gần bằng QPSK (siso AWGN 9,4 so với 10,0 dB), dù riêng đường dữ liệu (lượt "genie": biết trước thời điểm
và SIG) cho BPSK 5,5 dB. Nguyên nhân nằm ở khâu dò khung, không phải ở dữ liệu:

- Schmidl-Cox với ngưỡng 0,6 cần khoảng 9 dB mới dò được 99 % khung. Ở chế độ siso, nó còn cộng nhiễu của
  đầu vào RX1 (không dùng) vào phép đo.
- Thời điểm lấy theo tâm plateau: ở SNR thấp thường lệch quá vùng không ISI (−10..+3 mẫu).
- SIG là 48 bit BPSK không mã hóa, chỉ có CRC16, nên hỏng sớm hơn dữ liệu đã có RS khoảng 2 dB.

Cách sửa của v0.6 (định dạng khung giữ nguyên; bên phát không đổi):

1. `find()` chỉ dùng các anten mà chế độ dùng (siso: RX0) và ngưỡng hạ xuống 0,35 (`sync_threshold`).
   Báo động giả khi chỉ có nhiễu ở 1 MS/s: khoảng 2 lần/s ở siso, 0 ở 2 anten. Mỗi báo động giả chỉ tốn một lần
   giải SIG.
2. Định thời tinh bằng tương quan với LTF đã biết (2 symbol LTF, (w1 + w2) và (w1 − w2) gom cả hai TX),
   tìm trong ±24 mẫu quanh ước lượng thô. Chọn đường đến sớm nhất có ít nhất 30 % đỉnh, không nhất thiết là
   đường mạnh nhất. Lỗi định thời về 0 ở mọi SNR đo.
3. Cứu khung khi SIG hỏng (`sig_recover`): bên thu đã biết mode, điều chế (cấu hình) và interleaver hoặc điều chế ACM
   của SIG đúng gần nhất. Vì vậy nó vẫn giải payload, và chỉ giữ gói khi RS và CRC32 đều đúng. Bên thu v0.6 vẫn
   đọc khung của bên phát v0.5 (không đổi gì ở bên phát).

| siso, FER 1 % (dB) | v0.5 BPSK | v0.5 QPSK | v0.6 BPSK | v0.6 QPSK | genie BPSK / QPSK |
|---|---|---|---|---|---|
| AWGN, khung cố định | 9,4 | 10,0 | **5,8** | 9,7 | 5,5 / 9,6 |
| kênh cố định, khung cố định | 6,1 | 7,1 | **3,1** | 7,2 | 2,9 / 7,1 |
| AWGN, khung ACM | 9,4 | 10,0 | **5,7** | 10,0 | 5,5 / 9,9 |

`start_rx.sh --legacy-sync` quay về bộ thu v0.5 (ngưỡng 0,6, không định thời LTF, không cứu SIG) để so sánh
trên RF. Log RX đếm số khung được cứu (`sig_recovered` trong report).

## 6. Các chế độ MIMO

| Chế độ | TX × RX | Bộ thu | Đặc điểm |
|---|---|---|---|
| `siso` | 1×1 | chia cho h | Mốc so sánh, giống bản N210 |
| `simo` | 1×2 | MRC | Tăng khoảng 3 dB và có phân tập thu |
| `alamouti` | 2×2 | SFBC giải ZF chính xác từng cặp sóng mang | Phân tập bậc 4, tốc độ 1 luồng (**mặc định**) |
| `smux` | 2×2 | MMSE từng sóng mang | 2 luồng, gấp đôi tốc độ; cần H khả nghịch (cond thấp) |

Dung lượng TS sau RS8+8, với gap 80 mẫu:

| Chế độ / điều chế | Mẫu/khung | 0,5 MS/s | 1 MS/s | 2 MS/s |
|---|---|---|---|---|
| 1 luồng QPSK (siso/simo/alamouti) | 2080 | 181 kb/s | 362 kb/s | 723 kb/s |
| 1 luồng 16QAM | 1280 | 294 | 588 | 1175 |
| smux QPSK | 1280 | 294 | 588 | 1175 |
| smux 16QAM | 880 | 427 | 855 | 1709 |

Profile mặc định là alamouti QPSK ở 1 MS/s, video 640×360 / 15 fps, H.264 200 kb/s, TS 260 kb/s
(dùng khoảng 72% dung lượng, gần giống tỷ lệ của bản N210).

## 7. Đồng bộ trên một máy N321

- Source và sink UHD mở **cùng một thiết bị**, nên dùng chung clock 10 MHz nội và chung bộ đếm thời gian.
  Trước khi chạy, chương trình gọi `set_time_now(0)`, rồi đặt `set_start_time(1 s)` cho cả hai hướng.
  Nhờ vậy hai kênh TX khớp mẫu với nhau, hai kênh RX cũng vậy.
- Nếu còn lệch cố định ngắn hơn CP (16 mẫu = 16 µs ở 1 MS/s), sai lệch đó nằm gọn trong H(k) ước lượng từ LTF.
- Các LO của từng kênh có pha ngẫu nhiên sau mỗi lần tune, nhưng giữ cố định trong một lượt chạy, nên
  cũng nằm trong H. CFO giữa TX và RX gần như bằng 0 vì cùng một reference; phần dư được Schmidl-Cox và CPE bù.
- Muốn đồng pha qua các lần tune (beamforming, đo góc): dùng timed tune hoặc LO export của N321. Đây là việc sau.

## 8. Công suất và an toàn

- Đi qua cáp: **bắt buộc** khai báo attenuator thật ≥ 30 dB cho mỗi đường (`--attenuation-db`). Chương trình
  từ chối nếu `tx_gain > attenuation − 10`. Trường hợp xấu nhất (+20 dBm − 30 dB = −10 dBm) vẫn chỉ quanh
  ngưỡng P1dB của RX, không đi tới mức có thể làm hỏng máy.
- Dùng anten: chương trình giới hạn `tx_gain ≤ 30`. Trước khi phát cần **kiểm tra quy định băng tần nơi đặt máy**;
  915 MHz là băng ISM của Mỹ; ở Việt Nam các thiết bị cự ly ngắn kiểu LoRa thường dùng 920–923 MHz,
  nhưng cần đối chiếu văn bản quy định hiện hành trước khi phát qua anten. Đổi tần số chỉ cần `--freq`.
- `tx_rms` 0,1 là biên độ số, không phải dBm. Nên tăng gain dần, theo dõi SNR, EVM và `rx_rms` trong log.

## 9. Thời gian thực (CPU)

Đo trong máy sandbox (Xeon 2,1 GHz, 2 vCPU):

- Một khung tốn khoảng 0,2 ms để điều chế và khoảng 0,4–0,55 ms để giải điều chế theo lô.
- Ở 1 MS/s alamouti, nếu toàn bộ khung là DATA và không có khối kênh mô phỏng (gần với chế độ RF),
  chuỗi xử lý đạt khoảng 472 khung/s, so với 481 khung/s cần cho thời gian thực.
- Chế độ `sim` chậm hơn RF vì thêm khối kênh và FFmpeg cùng tranh 2 nhân CPU.
- Profile `config/light_500k.json` (0,5 MS/s, TS 140 kb/s) đã chạy kịp thời gian thực trong sandbox.

Máy Ubuntu của lab (HP 280 Pro G8) có nhân CPU nhanh hơn và nhiều nhân hơn, nhưng vẫn phải đo:
`python3 tools/bench_sim.py alamouti qpsk 1e6 --no-channel`. Nếu hệ số margin dưới khoảng 1,5 thì dùng
profile 500k. Khi Python không xử lý kịp, UHD sẽ in `O`/`U` (overrun/underrun) và mất khung.

## 10. Đã kiểm chứng / chưa kiểm chứng

Đã chạy trên GNU Radio 3.10.9 (Ubuntu 24.04) ở chế độ mô phỏng:

- `tests/test_modem.py` PASS. Cụ thể: định thời đúng đến ±2 mẫu; giải mã đúng cả 8 tổ hợp chế độ × điều chế
  khi có đa đường 2 tap và CFO. Kênh Rayleigh ở 12 dB, sau 150 khung: SISO mất 34 khung, Alamouti mất 0.
- Chạy video thật ở chế độ `sim`, 6–20 giây, kênh cố định hoặc Rayleigh, CFO 300 Hz, cả 4 chế độ: PASS.
  File TS thu trùng SHA-256 với file phát; ví dụ smux QPSK ở 420 kb/s nhận 5581/5581 DATA và 300/300 khung hình.
- **Chưa chạy trên N321 thật.** Phần `hardware.py` (subdev, timed start, gain, antenna) viết theo API gr-uhd
  3.10 và tài liệu UHD, nhưng cần lượt bring-up đầu tiên để xác nhận.

## 11. Thứ tự bring-up đề xuất

1. `tools/probe_n321.py`: kiểm tra phiên bản UHD và MPM, ping, MTU, sysctl.
2. `tools/probe_n321.py --stream`: chỉ thu, 2 kênh, không được có overrun.
3. `tools/bench_sim.py`: kiểm tra CPU còn dư.
4. Nối cáp chéo thẳng (A→A, B→B) có attenuator, chạy `--mimo siso`, sau đó `simo`, `alamouti`, `smux`.
5. Nối 2×2 đầy đủ qua bộ chia, hoặc dùng 4 anten ở 915 MHz cách nhau ≥ λ/2 ≈ 16 cm, rồi so sánh các chế độ.
6. Khi đã ổn: tăng Fs, dùng 16QAM hoặc smux, đo độ trễ từ camera đến màn hình.
