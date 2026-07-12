#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# HackRF One channelized viewer fixed on 1090 MHz (Mode S / ADS-B).
#
# Tunes the HackRF LO to 1091 MHz to keep the target off the DC/LO spur, then
# uses a frequency-translating FIR filter to shift, low-pass and decimate the
# 1090 MHz channel down to 4 Msps. All four displays (spectrum, waterfall,
# channel envelope, and averaged channel power) run on that channelized
# stream so the user can compare antenna orientations / RF gain settings and
# spot digital burst activity without demodulating.

import signal
import sys
from argparse import ArgumentParser

from PyQt5 import Qt, sip

from gnuradio import blocks, filter, gr, qtgui, soapy
from gnuradio.fft import window
from gnuradio.filter import firdes


# Fixed-tune channelized viewer for 1090 MHz (Mode S / ADS-B).
#
# We tune the HackRF LO to 1091 MHz so 1090 MHz sits at -1 MHz baseband, well
# clear of the HackRF's DC/LO spur, then use a frequency-translating FIR
# filter to shift the -1 MHz channel back to DC while low-pass filtering and
# decimating from 8 Msps down to 4 Msps for the displays.
TUNE_FREQ = 1091.0e6
CHANNEL_CENTER = 1090.0e6
FREQ_SHIFT = CHANNEL_CENTER - TUNE_FREQ  # -1 MHz
SAMP_RATE = 8.0e6
CHANNEL_RATE = 4.0e6
CHANNEL_DECIM = int(SAMP_RATE / CHANNEL_RATE)  # 2

# Envelope plot: smooth the |x| stream with a 64-sample moving average and
# decimate by 32 to a 125 kHz display stream. 64 samples of averaging at
# 4 Msps corresponds to ~16 us, shorter than an ADS-B extended-squitter
# frame (~120 us) so bursts are still visible as envelope bumps, but long
# enough to suppress the sample-by-sample noise on the envelope. (Individual
# 0.5 us ADS-B pulses are averaged away; this view is for activity, not bit
# recovery.)
SMOOTH_N = 64
SMOOTH_DECIM = 32
ENV_RATE = CHANNEL_RATE / SMOOTH_DECIM  # 125 kHz
TIME_WINDOW_S = 0.050  # 50 ms; try 0.020-0.100 to taste
TIME_SINK_POINTS = int(TIME_WINDOW_S * ENV_RATE)  # 3125 pts

# Envelope trigger. Noise floor sits around 0.04 in the raw magnitude, so
# 0.05 fires reliably on bursts without free-running on noise.
TRIGGER_LEVEL = 0.05


class antenna_viewer(gr.top_block, Qt.QWidget):

    def __init__(
        self,
        lna_gain=16,
        vga_gain=16,
        amp_enabled=False,
    ):
        gr.top_block.__init__(self, "HackRF 1090 MHz ADS-B Channel Viewer", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("HackRF 1090 MHz ADS-B Channel Viewer")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme("gnuradio-grc"))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)

        self.top_layout = Qt.QVBoxLayout(self)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "hackrf_976_antenna")
        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Variables
        ##################################################
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.amp_enabled = amp_enabled

        # Averaging window for the wideband power number sink. At the 4 Msps
        # channelized rate a length of 4096 samples is ~1 ms of energy per
        # output sample, which gives a stable, non-jittery number without
        # lagging behind antenna movements.
        self._power_avg_len = 4096
        # Decimate the mag-squared stream before averaging / log so the number
        # sink is not fed millions of updates per second.
        self._power_decim = 2048

        ##################################################
        # HackRF source
        ##################################################
        self.hackrf_source = soapy.source(
            "driver=hackrf", "fc32", 1, "", "", [""], [""]
        )
        self.hackrf_source.set_sample_rate(0, SAMP_RATE)
        self.hackrf_source.set_bandwidth(0, SAMP_RATE)
        self.hackrf_source.set_frequency(0, TUNE_FREQ)
        self.hackrf_source.set_gain(0, "AMP", bool(amp_enabled))
        self.hackrf_source.set_gain(0, "LNA", min(max(float(lna_gain), 0.0), 40.0))
        self.hackrf_source.set_gain(0, "VGA", min(max(float(vga_gain), 0.0), 62.0))

        ##################################################
        # Displays
        ##################################################
        self.freq_sink = qtgui.freq_sink_c(
            2048,
            window.WIN_BLACKMAN_hARRIS,
            CHANNEL_CENTER,
            CHANNEL_RATE,
            "Spectrum (1090 MHz channel)",
            1,
            None,
        )
        self.freq_sink.set_update_time(0.10)
        self.freq_sink.set_y_axis(-120, 0)
        self.freq_sink.set_y_label("Relative Gain", "dB")
        self.freq_sink.enable_autoscale(False)
        self.freq_sink.enable_grid(True)
        self.freq_sink.set_fft_average(0.2)
        self.freq_sink.enable_axis_labels(True)
        self.freq_sink.enable_control_panel(False)
        self._freq_sink_win = sip.wrapinstance(self.freq_sink.qwidget(), Qt.QWidget)

        self.waterfall_sink = qtgui.waterfall_sink_c(
            1024,
            window.WIN_BLACKMAN_hARRIS,
            CHANNEL_CENTER,
            CHANNEL_RATE,
            "Waterfall (1090 MHz channel)",
            1,
            None,
        )
        self.waterfall_sink.set_update_time(0.10)
        self.waterfall_sink.enable_grid(False)
        self.waterfall_sink.enable_axis_labels(True)
        self.waterfall_sink.set_intensity_range(-140, 10)
        self._waterfall_sink_win = sip.wrapinstance(self.waterfall_sink.qwidget(), Qt.QWidget)

        self.power_sink = qtgui.number_sink(
            gr.sizeof_float, 0, qtgui.NUM_GRAPH_HORIZ, 1, None
        )
        self.power_sink.set_update_time(0.10)
        self.power_sink.set_title("Avg Wideband Power (dBFS)")
        self.power_sink.set_min(0, -100)
        self.power_sink.set_max(0, 0)
        self.power_sink.set_color(0, "black", "red")
        self.power_sink.set_label(0, "Power")
        self.power_sink.set_unit(0, "dBFS")
        self.power_sink.set_factor(0, 1.0)
        self.power_sink.enable_autoscale(False)
        self._power_sink_win = sip.wrapinstance(self.power_sink.qwidget(), Qt.QWidget)

        # Wide enough to pass the full ~900 kHz ADS-B main lobe with
        # comfortable margin: at 4 Msps output the Nyquist is 2 MHz, so the
        # 900 kHz passband and 1 MHz stopband edge sit well inside the band
        # with no risk of aliasing.
        chan_taps = firdes.low_pass(
            1.0, SAMP_RATE, 900e3, 100e3, window.WIN_HAMMING, 6.76
        )
        self.chan_filter = filter.freq_xlating_fir_filter_ccc(
            CHANNEL_DECIM, chan_taps, FREQ_SHIFT, SAMP_RATE
        )
        self.mag = blocks.complex_to_mag(1)
        self.env_smoother = filter.fir_filter_fff(
            SMOOTH_DECIM, [1.0 / SMOOTH_N] * SMOOTH_N
        )
        self.time_sink = qtgui.time_sink_f(
            TIME_SINK_POINTS,
            ENV_RATE,
            f"1090 MHz Channel Envelope ({int(TIME_WINDOW_S * 1000)} ms, smoothed)",
            1,
            None,
        )
        self.time_sink.set_update_time(0.10)
        self.time_sink.set_y_axis(0.0, 0.5)
        self.time_sink.enable_autoscale(False)
        self.time_sink.enable_grid(True)
        self.time_sink.enable_axis_labels(True)
        self.time_sink.enable_control_panel(True)
        self.time_sink.set_trigger_mode(
            qtgui.TRIG_MODE_AUTO,
            qtgui.TRIG_SLOPE_POS,
            TRIGGER_LEVEL,
            0,
            0,
            "",
        )
        self._time_sink_win = sip.wrapinstance(self.time_sink.qwidget(), Qt.QWidget)

        ##################################################
        # Power measurement chain
        ##################################################
        self.mag_squared = blocks.complex_to_mag_squared(1)
        self.decim = blocks.keep_one_in_n(gr.sizeof_float, self._power_decim)
        self.moving_avg = blocks.moving_average_ff(
            self._power_avg_len, 1.0 / self._power_avg_len, 4000, 1
        )
        # 10*log10(|x|^2) = dBFS relative to full-scale complex input. Small
        # k offset avoids log(0) if the source is momentarily zero.
        self.nlog = blocks.nlog10_ff(10, 1, -1e-20)

        ##################################################
        # UI controls
        ##################################################
        controls = Qt.QGridLayout()
        row = 0

        controls.addWidget(
            Qt.QLabel(
                f"Tuned: {TUNE_FREQ / 1e6:g} MHz LO -> {CHANNEL_CENTER / 1e6:g} MHz "
                f"channel @ {CHANNEL_RATE / 1e6:g} Msps"
            ),
            row, 0, 1, 4,
        )

        self._amp_checkbox = Qt.QCheckBox("RF AMP (+14 dB)")
        self._amp_checkbox.setChecked(bool(amp_enabled))
        self._amp_checkbox.toggled.connect(self.set_amp)
        controls.addWidget(self._amp_checkbox, row, 4)

        row += 1

        controls.addWidget(Qt.QLabel("LNA (IF):"), row, 0)
        self._lna_slider = Qt.QSlider(Qt.Qt.Horizontal)
        self._lna_slider.setRange(0, 40)
        self._lna_slider.setSingleStep(8)
        self._lna_slider.setPageStep(8)
        self._lna_slider.setTickInterval(8)
        self._lna_slider.setTickPosition(Qt.QSlider.TicksBelow)
        self._lna_slider.setValue(int(lna_gain))
        self._lna_value_label = Qt.QLabel(f"{int(lna_gain)} dB")
        self._lna_value_label.setMinimumWidth(56)
        self._lna_slider.valueChanged.connect(self.set_lna_gain)
        controls.addWidget(self._lna_slider, row, 1, 1, 3)
        controls.addWidget(self._lna_value_label, row, 4)

        row += 1

        controls.addWidget(Qt.QLabel("VGA (BB):"), row, 0)
        self._vga_slider = Qt.QSlider(Qt.Qt.Horizontal)
        self._vga_slider.setRange(0, 62)
        self._vga_slider.setSingleStep(2)
        self._vga_slider.setPageStep(2)
        self._vga_slider.setTickInterval(10)
        self._vga_slider.setTickPosition(Qt.QSlider.TicksBelow)
        self._vga_slider.setValue(int(vga_gain))
        self._vga_value_label = Qt.QLabel(f"{int(vga_gain)} dB")
        self._vga_value_label.setMinimumWidth(56)
        self._vga_slider.valueChanged.connect(self.set_vga_gain)
        controls.addWidget(self._vga_slider, row, 1, 1, 3)
        controls.addWidget(self._vga_value_label, row, 4)

        self.top_layout.addLayout(controls)

        display_row = Qt.QHBoxLayout()
        display_row.addWidget(self._freq_sink_win, 3)
        display_row.addWidget(self._power_sink_win, 1)
        self.top_layout.addLayout(display_row, 2)
        self.top_layout.addWidget(self._waterfall_sink_win, 3)
        self.top_layout.addWidget(self._time_sink_win, 2)

        ##################################################
        # Connections
        ##################################################
        self.connect((self.hackrf_source, 0), (self.chan_filter, 0))
        self.connect((self.chan_filter, 0), (self.freq_sink, 0))
        self.connect((self.chan_filter, 0), (self.waterfall_sink, 0))
        self.connect((self.chan_filter, 0), (self.mag, 0))
        self.connect((self.mag, 0), (self.env_smoother, 0))
        self.connect((self.env_smoother, 0), (self.time_sink, 0))
        self.connect((self.chan_filter, 0), (self.mag_squared, 0))
        self.connect((self.mag_squared, 0), (self.decim, 0))
        self.connect((self.decim, 0), (self.moving_avg, 0))
        self.connect((self.moving_avg, 0), (self.nlog, 0))
        self.connect((self.nlog, 0), (self.power_sink, 0))

    ##################################################
    # Getters / setters
    ##################################################
    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "hackrf_976_antenna")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()
        event.accept()

    def set_lna_gain(self, lna_gain):
        self.lna_gain = int(lna_gain)
        self.hackrf_source.set_gain(0, "LNA", min(max(float(self.lna_gain), 0.0), 40.0))
        self._lna_value_label.setText(f"{self.lna_gain} dB")

    def set_vga_gain(self, vga_gain):
        self.vga_gain = int(vga_gain)
        self.hackrf_source.set_gain(0, "VGA", min(max(float(self.vga_gain), 0.0), 62.0))
        self._vga_value_label.setText(f"{self.vga_gain} dB")

    def set_amp(self, enabled):
        self.amp_enabled = bool(enabled)
        self.hackrf_source.set_gain(0, "AMP", self.amp_enabled)


def main(top_block_cls=antenna_viewer, options=None):
    if options is None:
        parser = ArgumentParser(
            description=(
                "HackRF One channelized viewer fixed on 1090 MHz (Mode S / "
                "ADS-B). Tunes the LO to 1091 MHz and digitally shifts the "
                "1090 MHz channel to DC before decimating to 4 Msps."
            )
        )
        parser.add_argument(
            "--lna", type=int, default=16,
            help="Initial LNA (IF) gain in dB, 0-40 step 8 (default: 16)",
        )
        parser.add_argument(
            "--vga", type=int, default=16,
            help="Initial VGA (baseband) gain in dB, 0-62 step 2 (default: 16)",
        )
        parser.add_argument(
            "--amp", action="store_true",
            help="Enable the HackRF front-end RF amplifier (~+14 dB)",
        )
        options = parser.parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(
        lna_gain=options.lna,
        vga_gain=options.vga,
        amp_enabled=options.amp,
    )

    tb.start()
    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()
        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()


if __name__ == "__main__":
    main()
