"""USRP N321 (2x2) setup for the GNU Radio UHD source/sink. No samples are sent here.

Same physical N321 for TX and RX: UHD caches the device, so the source and the
sink share one motherboard, one 10 MHz reference and one time base.
Both directions start at the same device time (timed start) so the two TX
channels and the two RX channels are each sample aligned. Any fixed residual
offset shorter than the 16-sample CP is absorbed by the per-path LTF estimate.
"""
import math
import time

RF_LINKS = ("cable", "antenna")


def port_label(channel, antenna):
    """Front-panel name of an N321 port: channel 0 = RF0 (UHD A:0), channel 1 = RF1 (B:0)."""
    return f"RF{int(channel)} {antenna}"


def antenna_summary(settings, role):
    """One line: which N321 ports need an antenna (or cable) for this mode. role: tx | rx | loopback."""
    s = settings
    tx, rx = s.tx_antenna, s.rx_antenna
    if role == "loopback":
        return (f"{port_label(0, tx)} + {port_label(1, tx)} phát, {port_label(0, rx)} + {port_label(1, rx)} thu "
                f"({s.mimo_mode}, {s.frequency / 1e6:.3f} MHz)")
    if s.acm and s.fb_link == "radio":
        if role == "tx":
            return (f"{port_label(0, tx)} phát video {s.frequency / 1e6:.3f} MHz | "
                    f"{port_label(1, 'RX2')} thu phản hồi {s.fb_freq / 1e6:.3f} MHz")
        return (f"{port_label(0, rx)} thu video {s.frequency / 1e6:.3f} MHz | "
                f"{port_label(1, 'TX/RX')} phát phản hồi {s.fb_freq / 1e6:.3f} MHz")
    if role == "tx":
        if s.n_tx == 2:
            return f"{port_label(0, tx)} + {port_label(1, tx)} phát video 2 kênh ({s.mimo_mode}, {s.frequency / 1e6:.3f} MHz)"
        return f"{port_label(0, tx)} phát video ({s.mimo_mode}, {s.frequency / 1e6:.3f} MHz) | RF1 không dùng"
    if s.n_rx_used == 2:
        return f"{port_label(0, rx)} + {port_label(1, rx)} thu video 2 kênh ({s.mimo_mode}, {s.frequency / 1e6:.3f} MHz)"
    return f"{port_label(0, rx)} thu video ({s.mimo_mode}, {s.frequency / 1e6:.3f} MHz) | RF1 không dùng"


def validate_rf(settings, rf_link, attenuation_db):
    s = settings
    if rf_link not in RF_LINKS:
        raise ValueError(f"rf_link must be one of {RF_LINKS}")
    notes = []
    if s.frequency < 450e6:
        notes.append("Below 450 MHz the N321 adds a mixer stage: TX max ~+10 dBm, "
                     "phase coherence not guaranteed.")
    if rf_link == "cable":
        if attenuation_db is None or not 30 <= attenuation_db <= 120:
            raise ValueError("Cable loopback needs real installed attenuation >= 30 dB per path "
                             "(N321 TX up to +20 dBm, RX P1dB about -15 dBm)")
        # worst case with full DAC swing: keep RX well below -15 dBm
        if s.tx_gain > attenuation_db - 10:
            raise ValueError("tx_gain too high for the declared attenuation (keep tx_gain <= att-10)")
    else:
        if attenuation_db is not None:
            raise ValueError("Antenna link: do not declare cable attenuation")
        if s.tx_gain > 30:
            raise ValueError("Antenna link starts with tx_gain <= 30 dB; check local regulations first")
    return notes


def make_radios(settings):
    from gnuradio import uhd
    s = settings
    args = uhd.stream_args(cpu_format="fc32", otw_format="sc16", channels=[0, 1])
    rx = uhd.usrp_source(s.device_args, args)
    tx = uhd.usrp_sink(s.device_args, args, "")
    return rx, tx


def configure(rx, tx, settings):
    from gnuradio import uhd
    s = settings
    report = {}
    for dev, kind in ((rx, "rx"), (tx, "tx")):
        dev.set_clock_source(s.clock_source, 0)
        dev.set_time_source(s.time_source, 0)
        dev.set_subdev_spec(s.subdev, 0)
        dev.set_samp_rate(s.sample_rate)
        gain = s.rx_gain if kind == "rx" else s.tx_gain
        antenna = s.rx_antenna if kind == "rx" else s.tx_antenna
        chans = []
        for ch in (0, 1):
            dev.set_center_freq(uhd.tune_request(s.frequency), ch)
            dev.set_gain(gain, ch)
            dev.set_antenna(antenna, ch)
            chans.append({"port": port_label(ch, dev.get_antenna(ch)), "freq": dev.get_center_freq(ch),
                          "gain": dev.get_gain(ch), "antenna": dev.get_antenna(ch)})
        rate = dev.get_samp_rate()
        if not math.isclose(rate, s.sample_rate, rel_tol=1e-9):
            raise ValueError(f"UHD coerced {kind} rate {s.sample_rate} -> {rate}")
        for ch in (0, 1):
            if abs(dev.get_center_freq(ch) - s.frequency) > 1e3:
                raise ValueError(f"{kind} {port_label(ch, antenna)} could not tune to {s.frequency}")
            if "lo_locked" in dev.get_sensor_names(ch):
                deadline = time.monotonic() + 3
                while not dev.get_sensor("lo_locked", ch).to_bool():
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{kind} {port_label(ch, antenna)} LO not locked")
                    time.sleep(0.05)
        report[kind] = {"rate": rate, "channels": chans,
                        "info": [dict(dev.get_usrp_info(c)) for c in (0, 1)]}
    report["master_clock_rate"] = rx.get_clock_rate(0)
    # Common time base, then aligned start of both streamers.
    rx.set_time_now(uhd.time_spec(0.0), uhd.ALL_MBOARDS)
    start = uhd.time_spec(s.start_delay_s)
    rx.set_start_time(start)
    tx.set_start_time(start)
    report["sync"] = {"clock_source": s.clock_source, "time_source": s.time_source,
                      "timed_start_s": s.start_delay_s}
    return report


# ---------------------------------------------------------------- two N321 (split TX / RX)
def make_radio(settings, kind):
    """Open ONE direction (kind 'tx' or 'rx') with both channels of this N321."""
    from gnuradio import uhd
    args = uhd.stream_args(cpu_format="fc32", otw_format="sc16", channels=[0, 1])
    if kind == "rx":
        return uhd.usrp_source(settings.device_args, args)
    return uhd.usrp_sink(settings.device_args, args, "")


def configure_direction(dev, settings, kind):
    """Configure one direction; the two channels of this device start at the same time.
    The other N321 has its own clock: CFO / timing are corrected by the receiver."""
    from gnuradio import uhd
    s = settings
    dev.set_clock_source(s.clock_source, 0)
    dev.set_time_source(s.time_source, 0)
    dev.set_subdev_spec(s.subdev, 0)
    dev.set_samp_rate(s.sample_rate)
    gain = s.rx_gain if kind == "rx" else s.tx_gain
    antenna = s.rx_antenna if kind == "rx" else s.tx_antenna
    chans = []
    for ch in (0, 1):
        dev.set_center_freq(uhd.tune_request(s.frequency), ch)
        dev.set_gain(gain, ch)
        dev.set_antenna(antenna, ch)
    rate = dev.get_samp_rate()
    if not math.isclose(rate, s.sample_rate, rel_tol=1e-9):
        raise ValueError(f"UHD coerced {kind} rate {s.sample_rate} -> {rate}: use master_clock_rate=200e6")
    for ch in (0, 1):
        if abs(dev.get_center_freq(ch) - s.frequency) > 1e3:
            raise ValueError(f"{kind} {port_label(ch, antenna)} could not tune to {s.frequency}")
        if "lo_locked" in dev.get_sensor_names(ch):
            deadline = time.monotonic() + 3
            while not dev.get_sensor("lo_locked", ch).to_bool():
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{kind} {port_label(ch, antenna)} LO not locked")
                time.sleep(0.05)
        chans.append({"port": port_label(ch, dev.get_antenna(ch)), "freq": dev.get_center_freq(ch),
                      "gain": dev.get_gain(ch), "antenna": dev.get_antenna(ch)})
    report = {"role": kind, "rate": rate, "channels": chans,
              "info": dict(dev.get_usrp_info(0)), "master_clock_rate": dev.get_clock_rate(0),
              "clock_source": s.clock_source, "time_source": s.time_source}
    if s.time_source == "gpsdo":
        report["gps_locked"] = bool(dev.get_mboard_sensor("gps_locked", 0).to_bool())
    t = time.monotonic()
    dev.set_time_now(uhd.time_spec(0.0), uhd.ALL_MBOARDS)
    report["t_set_monotonic"] = (t + time.monotonic()) / 2   # device time 0 (AirClock)
    dev.set_start_time(uhd.time_spec(s.start_delay_s))
    return report


# ---------------------------------------------------------------- per-channel streams (radio feedback)
def validate_tx_gain(gain, rf_link, attenuation_db, what="feedback"):
    """Same RF rules as validate_rf, for an extra transmitter (the RX machine's feedback)."""
    if rf_link == "cable":
        if attenuation_db is None or attenuation_db < 30 or gain > attenuation_db - 10:
            raise ValueError(f"{what}: cable needs --attenuation-db >= 30 and gain <= attenuation-10")
    elif gain > 30:
        raise ValueError(f"{what}: antenna link starts with gain <= 30 dB")


def make_stream(settings, kind, channels):
    """One streamer on the listed device channels (0 = RF0 = A:0, 1 = RF1 = B:0 of subdev 'A:0 B:0')."""
    from gnuradio import uhd
    args = uhd.stream_args(cpu_format="fc32", otw_format="sc16", channels=list(channels))
    if kind == "rx":
        return uhd.usrp_source(settings.device_args, args)
    return uhd.usrp_sink(settings.device_args, args, "")


def configure_streams(streams, settings):
    """streams: list of dicts {dev, kind, n, freq, gain, antenna, rate, name, chans}; all on ONE N321.
    chans: device channels of the streamer (0 = RF0, 1 = RF1), for the port names in the report.
    Each direction may use its own rate and frequency (Rhodium has separate TX and RX LOs).
    Ends with one common time base and a timed start of every streamer."""
    from gnuradio import uhd
    s = settings
    report = {}
    for st in streams:
        dev = st["dev"]
        dev.set_clock_source(s.clock_source, 0)
        dev.set_time_source(s.time_source, 0)
        dev.set_subdev_spec(s.subdev, 0)
        dev.set_samp_rate(st["rate"])
        rate = dev.get_samp_rate()
        if not math.isclose(rate, st["rate"], rel_tol=1e-9):
            raise ValueError(f"{st['name']}: UHD coerced rate {st['rate']} -> {rate}")
        chans = []
        for ch in range(st["n"]):
            dev.set_center_freq(uhd.tune_request(st["freq"]), ch)
            dev.set_gain(st["gain"], ch)
            dev.set_antenna(st["antenna"], ch)
            if abs(dev.get_center_freq(ch) - st["freq"]) > 1e3:
                raise ValueError(f"{st['name']} ({port_label(st.get('chans', [ch])[ch], st['antenna'])}) "
                                 f"could not tune to {st['freq']}")
            if "lo_locked" in dev.get_sensor_names(ch):
                deadline = time.monotonic() + 3
                while not dev.get_sensor("lo_locked", ch).to_bool():
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{st['name']} ({port_label(st.get('chans', [ch])[ch], st['antenna'])}) "
                                           "LO not locked")
                    time.sleep(0.05)
            port = port_label(st.get("chans", list(range(st["n"])))[ch], dev.get_antenna(ch))
            chans.append({"port": port, "freq": dev.get_center_freq(ch), "gain": dev.get_gain(ch),
                          "antenna": dev.get_antenna(ch)})
        report[st["name"]] = {"role": st["kind"], "rate": rate, "channels": chans,
                              "info": dict(dev.get_usrp_info(0))}
    first = streams[0]["dev"]
    report["master_clock_rate"] = first.get_clock_rate(0)
    report["clock_source"], report["time_source"] = s.clock_source, s.time_source
    if s.time_source == "gpsdo":
        report["gps_locked"] = bool(first.get_mboard_sensor("gps_locked", 0).to_bool())
    t = time.monotonic()
    first.set_time_now(uhd.time_spec(0.0), uhd.ALL_MBOARDS)
    report["t_set_monotonic"] = (t + time.monotonic()) / 2   # device time 0 (AirClock)
    for st in streams:
        st["dev"].set_start_time(uhd.time_spec(s.start_delay_s))
    return report
