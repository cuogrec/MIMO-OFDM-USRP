# Nhiễu ngoài và khảo sát phổ (v0.6.3)

## Ngày 26/11 đã thấy gì

- **Đầu nối SMA lỏng:** RF0 RX2 của máy thu bị lỏng làm SISO yếu khoảng 15 dB (`rx_rms_rf0` thấp hẳn RF1). Sau khi
  vặn chặt, Alamouti QPSK đạt 0 lỗi, EVM −30 dB.
- **SMUX QPSK** chạy được khi cond 13–15 dB (EVM −17..−19 dB), nhưng mất 1,6–3,8% gói. Lỗi dồn thành đợt, lặp lại
  khoảng 11–12 s, mỗi lần là một cặp đợt cách nhau 3 s. Máy phát sạch (`late_events` 0).
- **Nguyên nhân là nhiễu ngoài.** Khi tắt máy phát, `rx_rms` vẫn vọt lên 0,002–0,004 theo đúng nhịp đó. Phổ RF0 có
  dãy vạch hẹp cách nhau khoảng 75 kHz trên 920,5–921,5 MHz. Bộ thu còn báo khoảng 1000 khung "SIG hỏng" khi không
  có máy phát: Schmidl-Cox bắt nhầm tín hiệu có chu kỳ. Nhiễu đi theo một anten (anten B): đổi anten thì nhiễu chuyển
  sang cổng kia.
- **Lệch chế độ:** hai lần máy phát và máy thu chạy khác chế độ (ACM ↔ cố định), dẫn tới 100% SIG hỏng mà không có
  thông báo rõ.

`python3 tools/rf_fer_curve.py rf_data/20261126` vẽ `results/rf_20261126_timeline.png`: số codeword lỗi mỗi giây,
mức thu RF0/RF1 và cond của từng lượt. Các lượt SMUX cho thấy rõ những đợt lỗi lặp lại.

## Bộ thu chống nhiễu (v0.6.3, mặc định bật; `--no-interf-mitigation` = v0.6.2)

1. **Triệt nhiễu hẹp băng theo quyết định** (`Demodulator._nbi_clean`), chỉ chạy cho khung giải hỏng:
   - Dựng lại tín hiệu mong đợi từ quyết định cứng và kênh. Kênh lấy từ LTF, được làm trơn theo tần số (tối đa
     12 tap) và chỉ dùng các sóng mang yên lặng trong khoảng trống sau khung.
   - Lấy phần dư trên các cửa sổ FFT (bỏ các ô có quyết định đáng ngờ), cộng thêm LTF và khoảng trống.
   - Tìm từng vạch (CLEAN + tinh chỉnh + bình phương tối thiểu), trừ khỏi khung rồi giải lại.
2. **Nhớ và theo dõi vạch:** bộ vạch được giữ lại. Khi nó giải thích được năng lượng trong khoảng trống quanh khung,
   **mọi khung đều được làm sạch trước khi giải**: biên độ khớp từ hai khoảng trống, rẻ, chạy theo lô. Tần số được
   theo dõi qua bước pha giữa các khung. Nguồn phát có chu kỳ quay lại thì được xử lý ngay từ khung đầu.
3. **Chốt chặn:**
   - Bộ vạch chỉ được nhận khi giải thích phần lớn phần dư sạch.
   - Khoảng trống phải cao hơn nền nhiễu.
   - Có ngân sách CPU cho các lần cứu.
   - Khung chỉ hỏng vì bị cắt (TX khựng) không kích hoạt.
4. **Trọng số mềm theo sóng mang và theo symbol:** dùng phần dư sau quyết định và phổ khoảng trống. Sóng mang hay
   symbol cao hơn trung vị 6 dB bị xóa (RS errors-and-erasures xóa byte, LLR của LDPC ≈ 0).
5. **Xác nhận khung bằng LTF:** mọi ứng viên Schmidl-Cox phải có tương quan chuẩn hóa với LTF ≥ 0,25. Ứng viên bị
   loại được đếm vào `false_detections`.

**Mô phỏng** (`--sim-interf`, ở máy thu):
- `comb:SPACING:DB:PERIOD:GAP[:DUR[:PORT]]`: dãy vạch, bật theo cặp đợt.
- `tone:FREQ:DB`: một vạch liên tục.
- `burst:DB:DUR:PERIOD`: xung băng rộng.

Kèm `--sim-cond DB` để tạo kênh 2×2 có cond định trước. Mức hiệu chỉnh theo RF là `comb:75e3:-15:11:3:0.12`.

| SNR 30 dB, comb −15 dB (cặp đợt 0,12 s, chu kỳ 11 s) | CER v0.6.2 | CER v0.6.3 | Gói video thiếu v0.6.2 | v0.6.3 |
|---|---|---|---|---|
| **SMUX QPSK, cond 14 dB** | 2,34% | **0,09%** | 184/9328 (2,0%) | **8/9502 (0,08%)** |
| siso QPSK, kênh cố định | 0% | 0% | 0 | 0 |
| alamouti QPSK, kênh cố định | 0% | 0% | 0 | 0 |

Ngoại tuyến, với comb luôn bật trên SMUX QPSK, số khung mất trên 100:

| Mức comb | v0.6.2 | v0.6.3 |
|---|---|---|
| −20 dB | 34 | 0 |
| −18 dB | 100 | 2 |
| −15 dB | 100 | 4 |

Với `--fec ldpc` (3/4), SMUX không mất khung nào ngay cả khi không có bộ triệt.

**Thời gian thực:** khi không có nhiễu, bộ thu mới dò khung y như bộ thu cũ. Hệ số dư (`bench_sim`, không khối kênh)
giảm do phần trọng số theo sóng mang:

| Cấu hình | Trước | Sau |
|---|---|---|
| alamouti QPSK | ×2,7 | ×2,0 |
| SMUX QPSK | – | ×1,6 |
| SMUX 64QAM | ×1,48 | ×1,14 |

**Chẩn đoán trong report RX** (`diagnostics`):
- |h(rx,tx)| trung vị (dB) của 4 phần tử kênh.
- Mức nền khi không có khung (dBFS mỗi cổng).
- Tóm tắt nhiễu: sóng mang hay bị đánh dấu, số lần cứu/triệt, `false_detections`.
- Số overflow của UHD.
- `mode_mismatch`: log in "LỆCH CHẾ ĐỘ: máy phát đang chạy …, máy thu đang chạy …" khi hơn 90% SIG hỏng trong 2 s mà
  đọc được SIG theo định dạng hoặc chế độ khác.

## Khảo sát phổ trước khi đo: `tools/spectrum_survey.py` (CHỈ THU)

```bash
# N321 nối, KHÔNG có gì đang phát (cả máy phát lẫn phản hồi):
python3 tools/spectrum_survey.py --args addr=192.168.180.95 --gain 50 --seconds 25 --save-iq runs/survey_iq
python3 tools/spectrum_survey.py --iq runs/survey_iq/*.npz         # phân tích lại, không cần phần cứng
```

- Công cụ chỉ tạo `usrp_source`, không bao giờ mở luồng phát. Nó thu 2 kênh (RF0, RF1) ở các tâm 920,5 / 921 /
  921,5 / 922 / 922,5 MHz, mỗi tâm N giây.
- Ra `results/survey_<ngày>.png/.json`:
  - waterfall và công suất theo thời gian;
  - các vạch hẹp và khoảng cách giữa chúng;
  - các đợt nhiễu và chu kỳ ứng viên (tự tương quan; nên thu ít nhất 2 chu kỳ, tức `--seconds 25` cho nhịp 11 s);
  - so sánh RF0/RF1;
  - tâm video sạch nhất: cửa sổ 1 MHz trong 920–923 MHz, ngoài 922,7 MHz ± 1 MHz.

## Bài học

- **Vặn chặt SMA** ở cả hai đầu trước mỗi buổi đo. So `rx_rms_rf0` với `rx_rms_rf1` (`median_during_run.rf1_vs_rf0_db`,
  `diagnostics.h_db_median`); một cổng thấp hơn 10–15 dB thường là đầu nối, không phải kênh truyền.
- **Kiểm tra cond trước khi chạy SMUX:** cần cond dưới khoảng 15 dB. Nếu cao hơn, dùng alamouti.
- **Khảo sát phổ trước khi đo** (`spectrum_survey.py`). Chọn tần số video theo đề xuất; nếu nhiễu có chu kỳ thì so
  với đồng hồ để đoán nguồn phát.
- **Hai bên cùng chế độ:** `--mimo`, `--mod`, `--acm`, `--fec` phải giống nhau. Máy thu báo "LỆCH CHẾ ĐỘ" nếu khác.
- Có nhiễu mà vẫn cần SMUX: dùng `--fec ldpc`, chịu được nhiều sóng mang bị nhiễu hơn hẳn RS.
