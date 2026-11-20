# Đo đặc trưng hệ thống bằng mô phỏng (tools/sim_sweep.py)

Kết quả quét SNR 0–35 dB (bước 2 dB) của modem `ofdm321`, đo ngày 20/11/2026, v0.5.0. Không cần GNU Radio,
không cần radio: script gọi thẳng `Modulator` / `Demodulator` (numpy), có cả dò khung Schmidl-Cox và lệch tần
100 Hz. Chạy lại: `python3 tools/sim_sweep.py` (khoảng 13 phút trên 16 nhân), vẽ lại: `--plots-only`.

## Cách đo

| | Điều chế cố định | ACM |
|---|---|---|
| Khung | Khung cố định thật: mỗi khung 1 gói 212 B, interleaver `block` | Khung ACM 3760 mẫu, k codeword theo điều chế, interleaver `none` |
| Quét | 4 MIMO × 4 điều chế × 3 kênh × giải mềm bật/tắt × 18 mức SNR, 1000 khung mỗi điểm | 4 MIMO × 3 kênh × 18 mức SNR; Rayleigh: 12 khối kênh × 3 s mỗi điểm (khoảng 9600 khung) |
| Điều khiển | – | Chính `AcmController` của dự án; SNR_eff lọc EWMA 0,8/0,2, báo cáo mỗi 0,02 s, trễ vòng khoảng 40 ms, tỉ lệ codeword lỗi trên 0,1 s |

- **Kênh:**
  - `awgn`: H = I, tức hai đường song song, không có đường chéo.
  - `rayleigh`: H 2×2 ngẫu nhiên kèm tiếng vọng 0,3 trễ 3 mẫu. Với điều chế cố định, mỗi khung một kênh mới; với ACM, kênh giữ trong 3 s.
  - `fixed`: kênh cố định có tiếng vọng, giống `MimoChannelSim`.

  Cả ba được chuẩn hóa để E|h|² = 1.
- **SNR:** tổng công suất phát (cả hai TX khi chạy alamouti/smux) × E|h|² / công suất nhiễu ở mỗi anten thu.
- **FER:** tỉ lệ gói 212 B (codeword RS) bị mất.
- **Thông lượng hữu ích:** số gói đúng × 752 bit TS (sau mã ngoài RS8+8), quy về 1 MS/s. Phép tính chưa tính
  việc RS8+8 cứu lại gói mất.

## Các file

| File | Nội dung |
|---|---|
| `fer_sweep.csv` | 1728 điểm điều chế cố định: mimo, mod, channel, soft, snr_db, lost, fer, capacity_kbps, goodput_kbps |
| `acm_sweep.csv` | 216 điểm ACM: goodput_kbps, cer, tỉ lệ khung theo điều chế (share_*) |
| `summary.json` | SNR cần để FER ≤ 1%, lợi ích giải mềm, ACM so với điều chế cố định tốt nhất ở từng mức SNR |
| `fer_vs_snr.png` | FER theo SNR: 3 kênh × 4 chế độ MIMO, mỗi đường một điều chế (giải mềm bật) |
| `throughput_vs_snr.png` | Thông lượng hữu ích: điều chế cố định so với ACM (AWGN và Rayleigh) |
| `mimo_compare.png` | So sánh 4 chế độ MIMO trên Rayleigh: FER của QPSK, 16QAM và thông lượng ACM |
| `soft_gain.png` | Giải mềm và giải cứng trên Rayleigh; lợi ích (dB) ở FER 1% |

## SNR cần để FER ≤ 1% (dB, giải mềm)

Dấu "–" nghĩa là không đạt FER ≤ 1% trong dải 0–35 dB.

| Chế độ | AWGN: BPSK / QPSK / 16QAM / 64QAM | Rayleigh | Kênh cố định |
|---|---|---|---|
| siso | 9,2 / 9,7 / 16,2 / 22,1 | – / 32,9 / – / – | 6,1 / 6,6 / 13,7 / 19,4 |
| simo | 8,6 / 10,1 / 16,3 / 22,2 | 15,1 / 18,5 / 23,2 / 28,9 | 6,1 / 7,1 / 13,4 / 18,5 |
| alamouti | 9,8 / 11,1 / 16,6 / 22,5 | **12,0 / 13,3 / 20,5 / 25,3** | 7,4 / 7,7 / 14,2 / 19,9 |
| smux | 9,8 / 13,7 / 20,1 / 25,4 | 29,0 / 30,8 / – / – | 7,7 / 10,6 / 17,6 / 22,6 |

## Thông lượng hữu ích: ACM so với điều chế cố định tốt nhất (kb/s TS ở 1 MS/s)

| SNR | alamouti Rayleigh: ACM / cố định tốt nhất | siso Rayleigh | alamouti AWGN | smux AWGN |
|---|---|---|---|---|
| 10 dB | 235 / 302 (QPSK) | 148 / 144 | 200 / 291 | 397 / 359 |
| 16 dB | 474 / 469 (16QAM) | 242 / 266 | 387 / 362 | 400 / 588 |
| 20 dB | 688 / 579 (16QAM) | 371 / 384 | 387 / 588 | 775 / 843 |
| 26 dB | 1145 / 779 (64QAM) | 749 / 516 | 737 / 783 | 1474 / 1042 |
| 30 dB | 1165 / 783 (64QAM) | 891 / 652 | 1061 / 783 | 1474 / 1044 |

## Nhận xét chính

1. **Alamouti là chế độ bền nhất trên kênh fading.** Trên Rayleigh, QPSK đạt FER 1% ở 13,3 dB với alamouti,
   18,5 dB với simo, 32,9 dB với siso. Phân tập phát + thu tiết kiệm gần 20 dB so với một anten. siso trên Rayleigh
   có sàn lỗi: BPSK vẫn mất khoảng 1% gói ở 32–35 dB vì các lần fade sâu.
2. **smux cần SNR cao và kênh tốt.** Trên kênh cố định, smux cần thêm khoảng 3 dB so với alamouti cho cùng điều chế
   (QPSK 10,6 dB so với 7,7 dB), nhưng chở gấp đôi. Trên Rayleigh, smux QPSK cần 30,8 dB, còn 16QAM/64QAM không
   đạt 1% trong dải đo. Không dùng smux trên kênh fading.
3. **ACM trên Rayleigh cho thông lượng trung bình cao hơn điều chế cố định tốt nhất khoảng 12–16%** (trung bình tỉ
   số ACM / cố định tốt nhất trong 10–30 dB: alamouti 1,12; simo 1,12; siso 1,14). Ở 26 dB, alamouti + ACM đạt
   1145 kb/s so với 779 kb/s của 64QAM cố định. Một phần lợi ích ở SNR cao đến từ khung ACM hiệu quả hơn: 64QAM
   trong khung ACM chở tối đa 1200 kb/s, khung cố định chỉ 783 kb/s, vì mỗi gói cố định tốn 4 symbol mào đầu.
4. **Ngưỡng ACM (9/14/20/26 dB + biên 1 + trễ 1) quá thận trọng trên kênh không fading.** Trên AWGN, ACM chỉ đạt
   0,84–0,90 lần điều chế cố định tốt nhất trong 10–30 dB. Ví dụ ở 20 dB, ACM giữ QPSK (387 kb/s) trong khi
   16QAM cố định đã đạt 588 kb/s với FER < 1% (16QAM cần 16,6 dB, nhưng ACM chỉ lên 16QAM khi SNR_eff ≥ 22 dB).
   Ngưỡng được hiệu chỉnh trên Rayleigh; nên hiệu chỉnh lại trên phần cứng thật, hoặc giảm biên khi kênh ổn định.
5. **ACM + smux trên Rayleigh mất nhiều gói:** tỉ lệ codeword lỗi 30% ở 20 dB (86% khung là BPSK), vì ngưỡng
   SNR_eff không hợp với kênh smux kém điều kiện. alamouti + ACM giữ tỉ lệ codeword lỗi ≤ 2,1% trong toàn dải 10–35 dB. Khi
   dùng ACM, chọn alamouti (hoặc siso khi phản hồi qua radio).
6. **Giải mềm errors-and-erasures không đem lại lợi ích đo được trên ba kênh này.** Chênh lệch SNR ở FER 1% giữa
   giải cứng và giải mềm chỉ từ −0,9 đến +1,5 dB, nằm trong sai số thống kê của 1000 khung, với cả 3 kênh. Lợi ích
   đo được ở v0.2 (siso 18 dB: mất 38% → 25%) là trên kênh chọn lọc tần số mạnh (4 tap) và có nhiễu xung
   (`tools/fer_compare.py`). Với kênh tiếng vọng yếu và interleaver `block`, lỗi thường rơi cả khối, nên xóa byte
   không cứu được.
7. **Điều chế cấp cao trên kênh tốt:** trên kênh cố định, 64QAM đạt FER 1% ở 18,5–19,9 dB (siso/simo/alamouti).
   Bản RF 19/11 có SNR 24 dB, nên 64QAM khả thi ở chế độ SISO nếu kênh thật gần với kênh cố định.
8. **Lỗi tìm ra khi chạy sweep (đã sửa):** ở chế độ điều chế cố định 64QAM, nhánh giải mềm của bộ thu ném ngoại lệ
   (1984 bit codeword không chia hết cho 6 bit/ký hiệu) mỗi khi giải cứng thất bại. Nay đã sửa trong
   `ofdm321/modem.py`, có test `test_soft_decoding_every_modulation_fixed_frames`.

## Lưu ý khi đọc

- Kênh `awgn` là H = I: simo không có nhánh thu thứ hai (đường chéo bằng 0), nên không thấy lợi ích ghép kênh thu
  trên AWGN. Lợi ích của simo thể hiện trên Rayleigh.
- FER = 0 được vẽ ở 10⁻³. Với 1000 khung mỗi điểm, độ phân giải là 10⁻³, nên các giá trị quanh 1% sai số khoảng ±0,3%.
- Đường ACM trên Rayleigh dao động vì mỗi điểm chỉ có 12 lần rút kênh (mỗi lần 3 s). Muốn mượt hơn thì tăng
  `--acm-blocks`.
