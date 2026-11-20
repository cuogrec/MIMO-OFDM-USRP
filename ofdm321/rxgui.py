"""Receiver GUI (start_rx.sh --gui): constellation + spectrum of the two RX channels.

Constellation: equalised data symbols (after MRC / Alamouti / MMSE, CPE removed, before
de-mapping) of one good frame every `const_every` frames, taken from the MimoRxSink
message port "const" -> pdu_to_tagged_stream -> QT GUI Constellation Sink.
Spectrum: raw IQ of RF0 RX2 and RF1 RX2 (QT GUI Frequency Sink, 2 traces; front-panel port names).

Needs a desktop session (DISPLAY). Everything else in the receiver is unchanged.
"""
from gnuradio import gr, pdu, qtgui
from gnuradio.fft import window

try:
    from PyQt5 import Qt, sip
except ImportError:                                 # older PyQt5 layouts
    from PyQt5 import Qt
    import sip


def ensure_app():
    app = Qt.QApplication.instance()
    return app or Qt.QApplication(["ofdm321-rx"])


class RxGui:
    """Adds the GUI sinks to an existing top block `tb` that contains MimoRxSink `rx`.
    `iq` is a list of the two (block, port) endpoints that feed rx (for the spectrum)."""

    def __init__(self, tb, rx, iq, settings, title="N321 MIMO RX"):
        s = settings
        n = rx.demod.n_const
        self.win = Qt.QWidget()
        self.win.setWindowTitle(f"{title} | {s.mimo_mode} {s.modulation} {s.frequency / 1e6:.3f} MHz")
        lay = Qt.QGridLayout(self.win)

        self.p2s = pdu.pdu_to_tagged_stream(gr.types.complex_t, "packet_len")
        self.const = qtgui.const_sink_c(n, "Chòm sao sau cân bằng", 1, None)
        self.const.set_update_time(0.10)
        self.const.set_y_axis(-1.5, 1.5)
        self.const.set_x_axis(-1.5, 1.5)
        self.const.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.const.enable_autoscale(False)
        self.const.enable_grid(True)
        self.const.set_line_label(0, "data symbols")
        self.const.set_line_style(0, 0)
        self.const.set_line_marker(0, 0)
        self.const.set_line_color(0, "blue")
        self.const.set_line_alpha(0, 0.6)
        tb.msg_connect((rx, "const"), (self.p2s, "pdus"))
        tb.connect(self.p2s, self.const)

        self.freq = qtgui.freq_sink_c(1024, window.WIN_BLACKMAN_hARRIS, s.frequency, s.sample_rate,
                                      "Phổ thu RF0 / RF1", 2, None)
        self.freq.set_update_time(0.20)
        self.freq.set_y_axis(-120, -20)
        self.freq.enable_grid(True)
        self.freq.enable_autoscale(False)
        self.freq.set_fft_average(0.2)
        radio_fb = bool(getattr(s, "acm", False)) and getattr(s, "fb_link", "udp") == "radio"
        labels = (f"RF0 {s.rx_antenna}", "RF1 TX/RX (phát phản hồi)" if radio_fb else f"RF1 {s.rx_antenna}")
        for i, (lab, col) in enumerate(zip(labels, ("blue", "red"))):
            self.freq.set_line_label(i, lab)
            self.freq.set_line_color(i, col)
        for i, ep in enumerate(iq):
            tb.connect(ep, (self.freq, i))

        lay.addWidget(sip.wrapinstance(self.const.qwidget(), Qt.QWidget), 0, 0)
        lay.addWidget(sip.wrapinstance(self.freq.qwidget(), Qt.QWidget), 0, 1)
        self.status = Qt.QLabel("waiting for frames ...")
        self.status.setStyleSheet("font-family: monospace;")
        lay.addWidget(self.status, 1, 0, 1, 2)
        self.win.resize(1200, 560)
        self.blocks = [self.p2s, self.const, self.freq]

    def show(self):
        self.win.show()

    def set_status(self, text):
        self.status.setText(text)
