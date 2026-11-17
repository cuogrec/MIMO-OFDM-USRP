# Đấu nối mạch vòng 2×2 trên một N321

Cổng mặt trước của mỗi kênh: **TX/RX** (dùng để phát) và **RX2** (dùng để thu).
Kênh 0 là A:0, kênh 1 là B:0. Luôn tắt chương trình trước khi cắm hoặc rút cáp RF.

## A. Cáp thẳng (đầu tiên nên làm)

```
TX/RX (A:0) ──[att ≥30 dB]── RX2 (A:0)
TX/RX (B:0) ──[att ≥30 dB]── RX2 (B:0)
```

- Ma trận kênh gần như chéo (H ≈ diag), nên chạy được cả 4 chế độ, kể cả smux.
- Chạy theo thứ tự: `--mimo siso`, rồi `simo`, rồi `alamouti`, rồi `smux`.
  Với siso, RX B không có tín hiệu; bộ thu siso chỉ dùng RX A.
- Lệnh: `bash scripts/run_cable_2x2.sh --attenuation-db 30 --mimo siso --play`. Thay số 30 bằng số dB thật của attenuator.

## B. 2×2 đầy đủ qua bộ chia

```
TX/RX A ──att── splitter ─┬──► combiner ──► RX2 A
                           └──┐  ┌──────►
TX/RX B ──att── splitter ─┬──┼──┘
                           └──┴──► combiner ──► RX2 B
```

- Cần 4 bộ chia/gộp công suất 2 đường (dải 915 MHz, 50 Ω) và 2 attenuator. Suy hao thêm khoảng 7 dB.
- Nếu cáp các nhánh dài bằng nhau, H gần như có hạng 1 (cond lớn): smux sẽ kém, còn alamouti vẫn tốt.
  Có thể chèn một đoạn cáp dài hơn khoảng λ/4 (≈ 5,5 cm với cáp hệ số vận tốc 0,66) để làm lệch pha.

## C. Anten 915 MHz

- 4 anten 915 MHz: phát trên TX/RX A và B, thu trên RX2 A và B.
- λ ≈ 32,8 cm. Đặt **hai anten phát cách nhau ≥ 16 cm (λ/2)**, hai anten thu cũng vậy, để các đường kênh ít tương quan.
- Ở 27 MHz của bản N210, λ/2 là 5,5 m nên không thể làm được điều này.
- Bắt đầu với `--tx-gain 10`, tăng dần đến tối đa 30 trong khi xem SNR/EVM. Chỉ phát ở nơi được phép.
- Lệnh: `bash scripts/run_antenna_915.sh --mimo alamouti --play`.

## Đọc log

```
TX data 1034 q 0 | RX ok 2162 sig 3 rs 0 crc 0 | DATA 1034 missing 0 | SNR 22.3 dB EVM -23.3 dB cond 6.6 dB
```

- `q`: hàng đợi phát. Giá trị luôn bằng 64 nghĩa là link hoặc CPU không kịp.
- `sig` / `rs` / `crc`: số khung bị loại ở từng bước kiểm tra.
- `SNR`: tính từ H và nhiễu đo trên LTF. `EVM`: sai số sau cân bằng.
  QPSK cần EVM ≲ −12 dB; 16QAM cần ≲ −20 dB.
- `cond`: điều kiện của ma trận H tính bằng dB (chỉ khi có 2 TX). smux cần khoảng < 10–15 dB.
- PASS chỉ khi TS thu trùng SHA-256 với TS phát, không thiếu DATA nào, và đã nhận END.
