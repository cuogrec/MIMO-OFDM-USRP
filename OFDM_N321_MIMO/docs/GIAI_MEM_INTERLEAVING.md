# v0.2: giải mềm và interleaving

Hai cải tiến ở lớp vật lý, nằm trong `ofdm321/modem.py`. Định dạng gói ứng dụng, RS(248,216) và RS8+8 không đổi.

## Interleaving (phía phát chọn, phía thu tự theo)

Interleaver quyết định mỗi byte của từ mã RS nằm trên những sóng mang và symbol OFDM nào.

| Kiểu | Một byte RS nằm ở | Một sóng mang hỏng làm hỏng | Một symbol OFDM hỏng làm hỏng |
|---|---|---|---|
| `none` (v0.1) | 4 sóng mang liền nhau, 1 symbol | ~21 byte | ~12 byte |
| `block` (**mặc định**) | 2 sóng mang × 2 symbol | ~11 byte | ~24 byte |
| `carrier` | 1 sóng mang, 4 symbol liên tiếp | ~6 byte | ~48 byte |

RS(248,216) sửa được 16 byte lỗi, hoặc 32 byte nếu biết vị trí (erasure). `block` giữ cả hai trường hợp
trong giới hạn đó. `carrier` tốt nhất khi kênh fading theo tần số nhưng rất yếu với nhiễu xung.

- `block` chỉ thay đổi thứ tự ở chế độ 1 luồng QPSK (siso, simo, alamouti). Với 16QAM và smux, thứ tự thường
  đã có sẵn sự cân bằng này nên giữ nguyên.
- Mã interleaver nằm trong trường SIG của mỗi khung. Bộ thu đọc và tự giải đúng kiểu, nên **chỉ cần đặt ở TX**:
  `bash scripts/start_tx.sh --interleaver none|block|carrier`.
- **Tương thích:** RX v0.2 thu được TX v0.1. RX v0.1 **không** thu được TX v0.2 (trừ khi TX dùng
  `--interleaver none`). Hãy cập nhật cả hai folder cùng lúc.

## Giải mềm (phía thu)

1. Mỗi ký hiệu sau cân bằng có một độ tin cậy = khoảng cách tới biên quyết định × SNR sau cân bằng của sóng mang
   (và luồng) đó. SNR lấy từ MRC, từ ma trận ZF của SFBC, hoặc SINR của MMSE.
2. Độ tin cậy bị hạ xuống cho những symbol OFDM mà các sóng mang rỗng cho thấy nhiễu cao hơn nền (nhiễu xung).
3. Khi giải RS thường thất bại, bộ thu xóa dần các byte kém tin cậy nhất (2, 4, … 32 byte) và giải lại theo kiểu
   errors-and-erasures. Kết quả chỉ được nhận khi **cả 32 syndrome RS và CRC32 đều đúng**, nên không tạo gói sai.
4. Mỗi khung phải giải mềm tốn khoảng 2 ms. Bộ thu giới hạn phần này ở 30% thời gian thực; khi link quá xấu nó
   tạm bỏ qua giải mềm để không bị trễ (`soft_skipped` trong report).

Log RX có thêm `ok N (soft M)`: M là số khung được cứu nhờ giải mềm. Tắt để so sánh: `bash scripts/start_rx.sh --no-soft`.

## Kết quả đo (mô phỏng, `tools/fer_compare.py`)

Tỉ lệ mất khung (%), QPSK, kênh Rayleigh chọn lọc tần số 4 tap, mỗi khung một kênh mới, 200–300 khung mỗi điểm.

| Trường hợp | v0.1 (none + cứng) | none + mềm | block + cứng | **block + mềm** | carrier + mềm |
|---|---|---|---|---|---|
| SISO, 18 dB | 38,0 | 31,0 | 32,7 | **25,3** | 21,3 |
| SISO, 22 dB | 14,7 | 11,0 | 11,0 | **8,3** | 8,3 |
| SISO, 22 dB + nhiễu xung 1 symbol | 27,5 | 18,0 | 38,0 | **21,0** | 42,5 |
| Alamouti 2×2, 10 dB | 20,0 | 17,3 | 19,3 | **19,0** | 16,7 |
| Alamouti 2×2, 14 dB + nhiễu xung 1 symbol | 4,5 | 0,5 | 55,5 | **4,0** | 81,5 |

Chạy riêng (cấu hình none/carrier, cứng/mềm): SIMO 12 dB giảm từ 13,0% xuống 6,3%; smux 20 dB giảm từ 40,3% xuống 19,0%.

Nhận xét:

- Lợi rõ nhất ở SISO, SIMO và smux. Với Alamouti 2×2, phân tập bậc 4 đã làm kênh gần phẳng nên lợi nhỏ.
- Interleaving mà không có giải mềm thì **kém hơn** khi có nhiễu xung (55,5% so với 4,5%). Hai cải tiến phải đi cùng nhau.
- Mức lợi của kiểu giải mềm này (xóa theo độ tin cậy) vào khoảng 0,5–2 dB. Muốn lợi nhiều hơn phải thay mã trong
  bằng mã chập hoặc LDPC có giải mềm thật sự.
- Trong phòng lab ở băng 1 MHz, kênh thường gần phẳng theo tần số. Lợi thực tế trên RF phụ thuộc vào nhiễu băng hẹp
  và nhiễu xung của môi trường; chưa đo trên RF.

Tự chạy lại:

```bash
python3 tests/test_modem.py
python3 tools/fer_compare.py --mimo siso --snr 14 18 22
python3 tools/fer_compare.py --mimo alamouti --snr 14 20 --burst 1
```
