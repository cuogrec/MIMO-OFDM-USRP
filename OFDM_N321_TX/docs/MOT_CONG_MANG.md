# Chạy hai N321 khi PC chỉ có một cổng mạng (không switch)

Ý tưởng: **N321 #1 tự phát bằng CPU ARM của chính nó**, không cần nối mạng với PC trong lúc chạy. Cổng mạng duy nhất
của PC nối với **N321 #2 (thu)**.

1. PC tính sẵn toàn bộ dạng sóng phát (video → gói → FEC → khung OFDM 2×2) thành một file IQ.
2. Chép file đó sang N321 #1 một lần.
3. N321 #1 tự phát lại file đó qua UHD trên chính nó.

Kênh A = cổng **RF0**, kênh B = cổng **RF1** theo nhãn trên mặt máy N321.

```
              (bước A: chép file, dây mạng tạm cắm vào N321 #1)
PC ──USB console──► N321 #1: python3 n321_replay.py ─► TX/RX A+B ~~~ anten ~~~
PC ──SFP0 (cổng mạng duy nhất)──► N321 #2: RX2 A+B ─► start_rx.sh ─► video
```

Giới hạn:

- Không có ACM, vì N321 #1 không có đường phản hồi. Dùng điều chế cố định (`--mimo`, `--mod`).
- Video được mã hóa trước, không phát trực tiếp từ webcam.
- Video 20 s tạo file IQ khoảng 250 MB (cả 15 s khung IDLE chờ ở đầu).

Muốn có ACM và phát trực tiếp thì cách đơn giản vẫn là mua thêm **adapter USB 3.0 → Gigabit Ethernet** hoặc một switch gigabit.

## Bước A: tạo file IQ và chép sang N321 #1 (dây mạng cắm vào N321 #1)

```bash
cd ~/MIMO-OFDM-USRP/OFDM_N321_TX
python3 tools/make_iq.py --source file --input ~/Videos/clip.mp4 --seconds 20     # hoặc bỏ --source: testpattern
#   -> in ra folder runs/<giờ>_iq_alamouti_qpsk  (gọi là F)
F=$(ls -d runs/*_iq_* | tail -1)

# (tùy chọn) thử trước bằng mô phỏng, không cần radio:
#   terminal 1, folder RX:  bash scripts/sim_rx.sh --compare ~/MIMO-OFDM-USRP/OFDM_N321_TX/$F/report.json
#   terminal 2, folder TX:  python3 tools/iq_sim_send.py $F

ssh root@192.168.180.3 "mkdir -p /home/root/iq"
scp $F/* root@192.168.180.3:/home/root/iq/
ssh root@192.168.180.3 "cd /home/root/iq && python3 n321_replay.py --check"
```

`--check` phải in `CHECK_OK`, nghĩa là N321 có python3, numpy và module `uhd`. Nếu báo `MISSING`, gửi lại kết quả
để tìm cách khác.

## Bước B: chạy (dây mạng cắm vào N321 #2)

1. Rút dây mạng khỏi N321 #1, cắm sang SFP0 của N321 #2. Kiểm tra: `ping -c 2 192.168.180.95`.
2. **Terminal 1: máy thu chạy trước**

   ```bash
   cd ~/MIMO-OFDM-USRP/OFDM_N321_RX
   bash scripts/start_rx.sh --args addr=192.168.180.95,master_clock_rate=200e6 --play \
        --compare ~/MIMO-OFDM-USRP/OFDM_N321_TX/$F/report.json
   ```

3. **Terminal 2: khởi động phát trên N321 #1 qua console USB.** Cáp micro-USB nối cổng console của N321 #1 với PC.

   ```bash
   ls /dev/ttyUSB*                       # thường là ttyUSB2 (console Linux của N321)
   sudo screen /dev/ttyUSB2 115200       # Enter, đăng nhập root (không mật khẩu)
   cd /home/root/iq
   python3 n321_replay.py --freq 915e6 --gain 20
   ```

   Thoát screen: Ctrl+A rồi K.

File bắt đầu bằng 15 s khung IDLE để máy thu kịp khóa, sau đó mới tới video. Máy thu tự dừng khoảng 3 s sau khi nhận END.
Thành công khi in `RF_RX_VERIFIED` (SHA-256 trùng với file trên PC).

**Không có cáp console?** Hẹn giờ phát trước khi rút dây ở bước A:

```bash
ssh root@192.168.180.3 "cd /home/root/iq && nohup sh -c 'sleep 90; python3 n321_replay.py --freq 915e6 --gain 20' > replay.log 2>&1 &"
```

Trong 90 s đó, chuyển dây sang N321 #2 và chạy terminal 1. Lần sau cắm lại N321 #1, xem `replay.log`.

## Lưu ý

- Hai bên phải cùng `--mimo`, `--mod`, `--freq`, `--rate`. `make_iq.py` lấy theo `config/default.json` của folder TX
  hoặc tham số dòng lệnh, ví dụ `--mimo smux --mod 16qam`. Ở Việt Nam nên dùng `--freq 921e6` cho cả `n321_replay.py`
  và `start_rx.sh`.
- **Underflow (`underflow` tăng):** CPU ARM không đẩy kịp 1 MS/s. Chạy cả hai bên ở `--rate 500e3`. File IQ dùng chung
  được cho mọi tốc độ, chỉ cần hai bên giống nhau. Ở 500 kS/s video phát chậm gấp đôi, nhưng vẫn đủ để kiểm chứng.
- An toàn RF giống `start_tx.sh`: qua anten thì `--gain` ≤ 30; qua cáp thì thêm `--rf-link cable --attenuation-db 30`
  (gain ≤ suy hao − 10).
- `--repeat 0` phát lặp mãi (Ctrl+C để dừng). Máy thu chỉ nhận một lượt vì cùng session.
