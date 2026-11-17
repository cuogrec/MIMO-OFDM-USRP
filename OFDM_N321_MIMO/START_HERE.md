# Bắt đầu

1. `bash scripts/setup_ubuntu.sh`
2. Nối SFP+ 10G của máy vào SFP1 của N321 (image HG), rồi chạy `sudo bash scripts/setup_network_10g.sh <tên card>`
3. `python3 tools/probe_n321.py --stream`: lệnh này không phát RF
4. `python3 tests/test_modem.py` và `python3 tools/bench_sim.py alamouti qpsk 1e6 --no-channel`
5. `bash scripts/run_sim.sh --seconds 20 --play`
6. Nối cáp A→A và B→B qua attenuator ≥ 30 dB, rồi chạy
   `bash scripts/run_cable_2x2.sh --attenuation-db <số dB thật> --mimo siso --play`
7. Lần lượt chạy `--mimo simo`, `alamouti`, `smux`; sau đó chuyển sang dùng anten (docs/DAU_NOI_2x2.md)
