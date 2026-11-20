# OFDM_N321_TX — máy PHÁT (N321 #1)

Phát video OFDM 2×2 MIMO bằng 2 anten trên **RF0 TX/RX** (A:0) và **RF1 TX/RX** (B:0). Máy thu dùng folder `OFDM_N321_RX`.
Hướng dẫn đầy đủ (cách nối mạng, đổi IP, anten, đọc log): **[docs/HUONG_DAN_2_N321.md](docs/HUONG_DAN_2_N321.md)**.

Mã xử lý tín hiệu là gói chung `ofdm321` ở gốc repo (`../ofdm321`); folder này chỉ giữ `scripts/`, `config/`,
`docs/`, `grc/` và chương trình chạy. `tools/` và `tests/` là symlink về gốc repo, nên các lệnh bên dưới vẫn như cũ.
Cài lần đầu: `bash scripts/setup_ubuntu.sh` (apt + `pip install -e .`). Cập nhật: `git pull origin main`.

```bash
bash scripts/setup_ubuntu.sh                              # một lần
python3 tools/probe_n321.py --addr 192.168.180.3 --stream # không phát RF
python3 tests/test_modem.py

# máy RX chạy start_rx.sh TRƯỚC, sau đó:
bash scripts/start_tx.sh                                               # testpattern 30 s
bash scripts/start_tx.sh --source file --input ~/Videos/clip.mp4 --seconds 60
bash scripts/start_tx.sh --source webcam --input /dev/video0
bash scripts/start_tx.sh --mimo smux --freq 921e6                      # RX phải dùng cùng tham số
bash scripts/start_tx.sh --acm                                         # điều chế + bitrate video thích ứng, RX cũng --acm (docs/ACM.md)
bash scripts/start_tx.sh --mimo siso --acm --fb-link radio            # ACM, phản hồi qua radio (docs/PHAN_HOI_RADIO.md)
python3 tools/make_iq.py --source file --input clip.mp4                # PC 1 cổng mạng: N321 #1 tự phát (docs/MOT_CONG_MANG.md)

bash scripts/sim_tx.sh                                    # mô phỏng qua ZeroMQ (RX chạy sim_rx.sh trước)
python3 tools/plot_run.py --latest                        # đồ thị SNR / điều chế / bitrate của lượt vừa chạy
```

Mặc định: alamouti QPSK, 1 MS/s, 915 MHz, `tx_gain` 20 (tối đa 30 khi dùng anten), N321 ở `192.168.180.3`
(sửa trong `config/default.json` hoặc dùng `--args`). Kết thúc lượt chạy, chương trình in `TX_COMPLETE`, session và
sha256 của video; `runs/<lượt>/report.json` dùng để so sánh với RX.

## v0.5: bitrate video theo ACM

Với `--acm`, bitrate video đi theo điều chế (BPSK 150 … 64QAM 900 kb/s TS ở 1 MS/s), đổi tại khung khóa: hạ ngay,
tăng sau 5 s ổn định (`--no-adaptive-video` để giữ 260 kb/s). Khi hạ điều chế, máy phát phát lại các gói của vòng
phản hồi vừa qua (`--acm-resend`). Log in dòng `Cắm anten (...)` với tên cổng theo mặt máy (RF0/RF1); `report.json`
có `history` mỗi giây cho `tools/plot_run.py`. Cập nhật **cả hai** máy (`git pull origin main`). Chi tiết: [docs/ACM.md](docs/ACM.md).

## v0.2: giải mềm và interleaving

Bộ thu có giải mềm RS (xóa byte theo độ tin cậy); bộ phát có interleaver `block` mặc định, bộ thu tự theo.
Cập nhật **cả hai** folder TX và RX cùng lúc. Chi tiết và số đo: [docs/GIAI_MEM_INTERLEAVING.md](docs/GIAI_MEM_INTERLEAVING.md).
