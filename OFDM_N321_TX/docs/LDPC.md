# LDPC 802.11n làm mã trong (v0.6)

Từ v0.6, mã trong có thể là RS(248,216) (mặc định, như cũ) hoặc mã QC-LDPC của IEEE 802.11n. Gói ứng dụng
212 byte `OVN1` + CRC32 và mã ngoài RS8+8 giữ nguyên; chỉ thay phần bảo vệ từng gói trên kênh.

```bash
# hai bên PHẢI giống nhau (giống --mimo, --mod, --rate)
bash scripts/start_rx.sh ... --fec ldpc --ldpc-rate 3/4        # máy thu
bash scripts/start_tx.sh ... --fec ldpc --ldpc-rate 3/4        # máy phát
# ACM: chỉ cần --fec ldpc (khung ACM luôn dùng tỉ lệ 5/6 đục lỗ, xem dưới)
```

Nếu hai bên khác mã, máy thu bỏ khung như SIG sai, log in `KHÁC FEC n khung`, và `report.json` ghi
`fec.frames_with_other_fec`. Khi so với report của máy phát (`--compare tx/report.json`), `fec_match` phải là `true`
thì mới có `RF_RX_VERIFIED` / `SIM_RX_VERIFIED`.

## Mã

- **12 mã của chuẩn:** n = 648 / 1296 / 1944 (Z = 27 / 54 / 81), tỉ lệ 1/2, 2/3, 3/4, 5/6. Ma trận cơ sở (24 cột khối)
  lấy theo IEEE 802.11-2012 Annex F. Lúc import, `ofdm321/ldpc.py` kiểm tra cấu trúc phần parity: cột đầu có hệ số
  dịch a, 0, a, các cột còn lại là đường chéo kép. Test dựng H dày từ ma trận cơ sở (ma trận đơn vị dịch vòng)
  và kiểm tra H·c = 0 cho cả 12 mã.
- **Mã hóa:** không cần ma trận sinh. λᵢ là phần thông tin của từng hàng khối; p₀ = Σλᵢ; p₁ = λ₀ + P^a·p₀; sau đó
  pᵢ₊₁ = λᵢ + pᵢ (cộng thêm p₀ ở hàng giữa).
- **Giải mã:** min-sum chuẩn hóa theo lớp (mỗi hàng khối là một lớp, α = 0,75), tối đa 20 vòng (`ldpc_iter`). Bộ giải
  vector hóa numpy trên (codeword, cạnh, Z), giải mọi gói của cả lô khung (tới 16 khung) trong một lần gọi, và
  dừng riêng từng codeword khi syndrome bằng 0. Trước khi lặp, bộ giải thử mã hóa lại phần thông tin quyết định
  cứng: nếu khớp mọi bit đã phát thì gói đó không cần vòng lặp nào. Đây là trường hợp thường gặp khi kênh tốt.

## Ghép gói vào khung

Một gói = 212 B + CRC32 = 1728 bit thông tin, chia đều cho 2 codeword. Phần thông tin còn thừa của mỗi codeword
là bit 0 đã biết (rút ngắn, không phát). Bộ mã được chọn theo tỉ lệ sao cho ít bit phát nhất:

| `--ldpc-rate` | Mã | Bit/gói | Tỉ lệ thực | Ký hiệu dữ liệu, QPSK 1 luồng | TS ở 1 MS/s, alamouti QPSK | so với RS |
|---|---|---|---|---|---|---|
| RS(248,216) | – | 1984 | 0,871 | 21 | 362 kb/s | 100 % |
| 1/2 | n 1944 × 2 | 3672 | 0,471 | 39 | 214 kb/s | 59 % |
| 2/3 | n 1296 × 2 | 2592 | 0,667 | 27 | 294 kb/s | 81 % |
| 3/4 (mặc định) | n 1296 × 2 | 2376 | 0,727 | 25 | 313 kb/s | 86 % |
| 5/6 | n 1296 × 2 | 2160 | 0,800 | 23 | 336 kb/s | 93 % |

TS ở 1 MS/s cho các điều chế khác (alamouti/siso, kb/s): BPSK 200 (RS) / 115 / 159 / 171 / 188; 16QAM 588 / 376 /
495 / 522 / 553; 64QAM 783 / 522 / 671 / 671 / 723 (RS / 1/2 / 2/3 / 3/4 / 5/6). TS mux mặc định là 260 kb/s:
LDPC 1/2 với QPSK không đủ chỗ (máy phát in cảnh báo); khi đó dùng 16QAM, tăng `--rate` hoặc hạ `ts_muxrate`.

**Khung ACM:** khung luôn dài 42 symbol, và mỗi gói có đúng 2016 bit, bằng chỗ của một codeword RS. Vì vậy số gói/khung
(BPSK 1, QPSK 2, 16QAM 4, 64QAM 6; smux gấp đôi), dung lượng và bậc bitrate video giữ nguyên. Mã dùng là
n = 1296, tỉ lệ 5/6, 2 codeword/gói, rút ngắn rồi đục lỗ 72 bit parity mỗi codeword. Bit đục lỗ rải đều trên phần
đường chéo kép, không đụng p₀. Tỉ lệ thực 1728/2016 = 0,857, gần bằng RS (0,871): so sánh ACM + LDPC với ACM + RS
là so ở cùng dung lượng.

## LLR, cân bằng và interleaver

- `modem.bit_llr`: LLR max-log cho BPSK/QPSK/16QAM/64QAM. Gray theo từng trục, nên mỗi trục tính riêng:
  LLR = SNR × (min khoảng cách² tới điểm có bit 1 − min khoảng cách² tới điểm có bit 0). SNR là SNR sau cân bằng của từng
  sóng mang/luồng (MRC, SFBC ZF chính xác theo cặp, MMSE bỏ bias), chính là trọng số đã dùng cho giải mềm RS, đã
  nhân hệ số giảm khi symbol đó có nhiễu xung.
- Interleaver bit ngẫu nhiên cố định (`modem.bit_perm`) trên toàn bộ bit của khung, đặt giữa LDPC và bộ ánh xạ. Nó trải
  mỗi codeword lên mọi sóng mang và symbol, và tách các bit của cùng một điểm chòm sao. Với LDPC, interleaver symbol
  (`block`/`carrier`) không dùng (SIG ghi `none`).

## Báo loại mã trong SIG (tương thích ngược)

- Byte 1 của SIG còn trống bit 6 (mã chế độ chỉ dùng bit 4–5). **Bit 6 = 1 nghĩa là LDPC.** Máy thu v0.5 so sánh cả
  byte nên coi khung LDPC là SIG sai và bỏ (không giải nhầm). Máy thu v0.6 với `--fec rs` vẫn đọc khung RS của bên
  phát cũ như trước.
- Khung cố định: tỉ lệ LDPC nằm ở 2 bit cao của số thứ tự 16 bit (số thứ tự còn 14 bit; số này chỉ dùng để thống
  kê). Khung ACM: chỉ có cờ LDPC (tỉ lệ cố định 5/6 đục lỗ).

## Thời gian thực

Đo trên máy thu (HP 280 Pro G8), một tiến trình Python:

- Riêng bộ giải (`python3 tools/bench_sim.py --decoder`, 64 gói mỗi lần gọi, BPSK AWGN):

| Mã | Eb/N0 (dB) | Vòng TB | codeword/s | Mb/s (bit mã) | gói/s |
|---|---|---|---|---|---|
| n 1944, 1/2 | 2,0 / 4,0 | 6,4 / 2,6 | 1384 / 3064 | 2,7 / 6,0 | 692 / 1532 |
| n 1296, 2/3 | 2,8 / 4,8 | 3,8 / 1,8 | 3230 / 6244 | 4,2 / 8,1 | 1615 / 3122 |
| n 1296, 3/4 | 3,4 / 5,4 | 2,8 / 1,3 | 4623 / 8325 | 6,0 / 10,8 | 2312 / 4162 |
| n 1296, 5/6 | 4,2 / 6,2 | 1,9 / 1,0 | 6423 / 9956 | 8,3 / 12,9 | 3212 / 4978 |
| ACM (5/6 → 2016 bit) | 4,6 / 6,6 | 3,1 / 1,6 | 4336 / 7073 | 5,6 / 9,2 | 2168 / 3536 |

- Cả chuỗi TX → RX trong GNU Radio, không có khối kênh (gần với RF), `tools/bench_sim.py MIMO MOD 1e6 --no-channel
  --fec ldpc`. Hệ số dư so với thời gian thực ở 1 MS/s:

| Cấu hình | RS | LDPC |
|---|---|---|
| alamouti QPSK (LDPC 3/4) | ×2,74 | ×2,31 |
| alamouti QPSK, LDPC 1/2 / 5/6 | – | ×2,47 / ×2,27 |
| siso BPSK, LDPC 1/2 | ×4,47 | ×5,13 |
| smux 64QAM, LDPC 5/6 | ×1,48 | **×1,17** |
| ACM alamouti 64QAM | ×2,39 | ×2,16 |
| ACM smux 64QAM | ×2,08 | **×1,68** |
| ACM siso 16QAM (phản hồi radio) | – | ×2,85 |

  Kết luận: **1 MS/s giữ được** với mọi cấu hình siso/simo/alamouti. smux 64QAM là sát nhất (×1,17 cố định,
  ×1,68 ACM); nếu thấy `O`/`U` của UHD hoặc `lost` tăng thì dùng `--rate 800e3`. Khi SNR thấp, bộ giải cần nhiều
  vòng hơn, nhưng lúc đó ACM đã hạ xuống BPSK/QPSK (ít gói mỗi khung). Trong `work()` không có vòng Python theo
  từng mẫu hay từng bit: mọi phép tính đều chạy trên mảng numpy của cả lô.

## Kết quả mô phỏng (`results/README.md`, `results/ldpc_vs_rs.png`)

SNR cần để FER ≤ 1% (dB), giải mềm RS so với LDPC; trong ngoặc là lợi ích:

| | RS | LDPC 1/2 | LDPC 3/4 | LDPC 5/6 |
|---|---|---|---|---|
| siso AWGN BPSK / QPSK | 5,5 / 9,9 | 2,4 / 4,2 | 2,6 / 6,4 | 4,1 (1,4) / 7,5 (2,4) |
| siso AWGN 16QAM / 64QAM | 16,2 / 22,1 | 8,5 / 12,4 | 12,2 / 17,3 | 12,6 (3,6) / 18,3 (3,8) |
| alamouti Rayleigh BPSK / QPSK | 11,4 / 13,1 | 6,6 / 8,6 | 8,0 / 10,3 | 8,9 (2,5) / 10,8 (2,3) |
| alamouti Rayleigh 16QAM / 64QAM | 20,5 / 25,3 | 12,6 / 16,0 | 16,2 / 20,6 | 16,1 (4,4) / 21,4 (3,9) |

- Ở tỉ lệ gần RS nhất (5/6), LDPC lợi 1,4–3,9 dB (AWGN) và 2,3–4,4 dB (Rayleigh alamouti); siso trên Rayleigh lợi
  6,7–13 dB nhờ interleaver bit.
- ACM + LDPC (cùng dung lượng khung với RS): thông lượng trung bình 10–30 dB cao hơn khoảng 13%; ngưỡng SNR_eff mới
  QPSK 12, 16QAM 19, 64QAM 24 dB (`acm.THRESHOLDS_BY_FEC["ldpc"]`).
- Chọn tỉ lệ: link tốt và cần dung lượng → RS hoặc LDPC 5/6; link yếu (siso, fading, SNR thấp) → LDPC 3/4 hoặc 1/2,
  đổi lại dung lượng (bảng ghép gói ở trên).
