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
            chans.append({"freq": dev.get_center_freq(ch), "gain": dev.get_gain(ch),
                          "antenna": dev.get_antenna(ch)})
        rate = dev.get_samp_rate()
        if not math.isclose(rate, s.sample_rate, rel_tol=1e-9):
            raise ValueError(f"UHD coerced {kind} rate {s.sample_rate} -> {rate}")
        for ch in (0, 1):
            if abs(dev.get_center_freq(ch) - s.frequency) > 1e3:
                raise ValueError(f"{kind} ch{ch} could not tune to {s.frequency}")
            if "lo_locked" in dev.get_sensor_names(ch):
                deadline = time.monotonic() + 3
                while not dev.get_sensor("lo_locked", ch).to_bool():
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{kind} ch{ch} LO not locked")
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
            raise ValueError(f"{kind} ch{ch} could not tune to {s.frequency}")
        if "lo_locked" in dev.get_sensor_names(ch):
            deadline = time.monotonic() + 3
            while not dev.get_sensor("lo_locked", ch).to_bool():
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{kind} ch{ch} LO not locked")
                time.sleep(0.05)
        chans.append({"freq": dev.get_center_freq(ch), "gain": dev.get_gain(ch),
                      "antenna": dev.get_antenna(ch)})
    report = {"role": kind, "rate": rate, "channels": chans,
              "info": dict(dev.get_usrp_info(0)), "master_clock_rate": dev.get_clock_rate(0),
              "clock_source": s.clock_source, "time_source": s.time_source}
    if s.time_source == "gpsdo":
        report["gps_locked"] = bool(dev.get_mboard_sensor("gps_locked", 0).to_bool())
    dev.set_time_now(uhd.time_spec(0.0), uhd.ALL_MBOARDS)
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
    """One streamer on the listed device channels (0 = A, 1 = B of subdev 'A:0 B:0')."""
    from gnuradio import uhd
    args = uhd.stream_args(cpu_format="fc32", otw_format="sc16", channels=list(channels))
    if kind == "rx":
        return uhd.usrp_source(settings.device_args, args)
    return uhd.usrp_sink(settings.device_args, args, "")


def configure_streams(streams, settings):
    """streams: list of dicts {dev, kind, n, freq, gain, antenna, rate, name}; all on ONE N321.
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
                raise ValueError(f"{st['name']} could not tune to {st['freq']}")
            if "lo_locked" in dev.get_sensor_names(ch):
                deadline = time.monotonic() + 3
                while not dev.get_sensor("lo_locked", ch).to_bool():
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"{st['name']} LO not locked")
                    time.sleep(0.05)
            chans.append({"freq": dev.get_center_freq(ch), "gain": dev.get_gain(ch),
                          "antenna": dev.get_antenna(ch)})
        report[st["name"]] = {"role": st["kind"], "rate": rate, "channels": chans,
                              "info": dict(dev.get_usrp_info(0))}
    first = streams[0]["dev"]
    report["master_clock_rate"] = first.get_clock_rate(0)
    report["clock_source"], report["time_source"] = s.clock_source, s.time_source
    if s.time_source == "gpsdo":
        report["gps_locked"] = bool(first.get_mboard_sensor("gps_locked", 0).to_bool())
    first.set_time_now(uhd.time_spec(0.0), uhd.ALL_MBOARDS)
    for st in streams:
        st["dev"].set_start_time(uhd.time_spec(s.start_delay_s))
    return report
