#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# HackRF One spectrum viewer tuned for 978 MHz antenna evaluation.
#
# Shows an FFT plot, a waterfall, a narrowband time-domain envelope, and an
# averaged wideband power reading so the user can compare antenna orientations
# / RF gain settings and spot digital burst activity without demodulating.

import signal
import sys
from argparse import ArgumentParser

from PyQt5 import Qt, sip

from gnuradio import blocks, filter, gr, qtgui, soapy
from gnuradio.fft import window
from gnuradio.filter import firdes


HACKRF_SAMP_RATES_MSPS = (2.0, 4.0, 8.0, 10.0, 12.5, 16.0, 20.0)


class antenna_viewer(gr.top_block, Qt.QWidget):

    def __init__(
        self,
        center_freq=978.0e6,
        samp_rate=8.0e6,
        lna_gain=16,
        vga_gain=16,
        amp_enabled=False,
    ):
        gr.top_block.__init__(self, "HackRF 978 MHz Antenna Viewer", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("HackRF 978 MHz Antenna Viewer")
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
        self.samp_rate = samp_rate
        self.center_freq = center_freq
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain
        self.amp_enabled = amp_enabled

        # Averaging window for the wideband power number sink. At 8 Msps a
        # length of 8192 samples is ~1 ms of energy per output sample, which
        # gives a stable, non-jittery number without lagging behind antenna
        # movements.
        self._power_avg_len = 8192
        # Decimate the mag-squared stream before averaging / log so the number
        # sink is not fed millions of updates per second.
        self._power_decim = 4096
        # Narrowband branch for the time plot: ~1 MHz channel at ~1 Msps so
        # digital bursts (e.g. UAT at 978 MHz) are visible in the envelope.
        self._chan_decim = max(1, int(samp_rate // 1e6))
        self._chan_rate = samp_rate / self._chan_decim

        ##################################################
        # HackRF source
        ##################################################
        self.hackrf_source = soapy.source(
            "driver=hackrf", "fc32", 1, "", "", [""], [""]
        )
        self.hackrf_source.set_sample_rate(0, samp_rate)
        self.hackrf_source.set_bandwidth(0, samp_rate)
        self.hackrf_source.set_frequency(0, center_freq)
        self.hackrf_source.set_gain(0, "AMP", bool(amp_enabled))
        self.hackrf_source.set_gain(0, "LNA", min(max(float(lna_gain), 0.0), 40.0))
        self.hackrf_source.set_gain(0, "VGA", min(max(float(vga_gain), 0.0), 62.0))

        ##################################################
        # Displays
        ##################################################
        self.freq_sink = qtgui.freq_sink_c(
            2048,
            window.WIN_BLACKMAN_hARRIS,
            center_freq,
            samp_rate,
            "Spectrum",
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
            center_freq,
            samp_rate,
            "Waterfall",
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

        chan_taps = firdes.low_pass(
            1.0, samp_rate, 500e3, 200e3, window.WIN_HAMMING, 6.76
        )
        self.chan_filter = filter.fir_filter_ccf(self._chan_decim, chan_taps)
        self.mag = blocks.complex_to_mag(1)
        self.time_sink = qtgui.time_sink_f(
            2048,
            self._chan_rate,
            "Channel Envelope (~1 MHz)",
            1,
            None,
        )
        self.time_sink.set_update_time(0.10)
        self.time_sink.set_y_axis(-0.1, 1.0)
        self.time_sink.enable_autoscale(True)
        self.time_sink.enable_grid(True)
        self.time_sink.enable_axis_labels(True)
        self.time_sink.enable_control_panel(False)
        self.time_sink.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
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

        controls.addWidget(Qt.QLabel("Frequency:"), row, 0)
        self._freq_spinbox = Qt.QDoubleSpinBox()
        self._freq_spinbox.setDecimals(3)
        self._freq_spinbox.setSingleStep(0.1)
        self._freq_spinbox.setRange(1.0, 6000.0)
        self._freq_spinbox.setSuffix(" MHz")
        self._freq_spinbox.setValue(center_freq / 1e6)
        self._freq_spinbox.valueChanged.connect(lambda mhz: self.set_center_freq(mhz * 1e6))
        controls.addWidget(self._freq_spinbox, row, 1)

        controls.addWidget(Qt.QLabel("Sample rate:"), row, 2)
        self._samp_rate_combo = Qt.QComboBox()
        for msps in HACKRF_SAMP_RATES_MSPS:
            self._samp_rate_combo.addItem(f"{msps:g} Msps", msps * 1e6)
        idx = self._samp_rate_combo.findData(samp_rate)
        if idx < 0:
            self._samp_rate_combo.addItem(f"{samp_rate / 1e6:g} Msps", samp_rate)
            idx = self._samp_rate_combo.count() - 1
        self._samp_rate_combo.setCurrentIndex(idx)
        self._samp_rate_combo.currentIndexChanged.connect(
            lambda i: self.set_samp_rate(self._samp_rate_combo.itemData(i))
        )
        controls.addWidget(self._samp_rate_combo, row, 3)

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
        self.connect((self.hackrf_source, 0), (self.freq_sink, 0))
        self.connect((self.hackrf_source, 0), (self.waterfall_sink, 0))
        self.connect((self.hackrf_source, 0), (self.chan_filter, 0))
        self.connect((self.chan_filter, 0), (self.mag, 0))
        self.connect((self.mag, 0), (self.time_sink, 0))
        self.connect((self.hackrf_source, 0), (self.mag_squared, 0))
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

    def set_center_freq(self, center_freq):
        self.center_freq = float(center_freq)
        self.hackrf_source.set_frequency(0, self.center_freq)
        self.freq_sink.set_frequency_range(self.center_freq, self.samp_rate)
        self.waterfall_sink.set_frequency_range(self.center_freq, self.samp_rate)

    def set_samp_rate(self, samp_rate):
        self.samp_rate = float(samp_rate)
        self.hackrf_source.set_sample_rate(0, self.samp_rate)
        self.hackrf_source.set_bandwidth(0, self.samp_rate)
        self.freq_sink.set_frequency_range(self.center_freq, self.samp_rate)
        self.waterfall_sink.set_frequency_range(self.center_freq, self.samp_rate)
        self.time_sink.set_samp_rate(self.samp_rate / self._chan_decim)

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
            description="HackRF One spectrum viewer for antenna evaluation."
        )
        parser.add_argument(
            "--frequency", type=float, default=978.0,
            help="Center frequency in MHz (default: 978.0)",
        )
        parser.add_argument(
            "--samp-rate", type=float, default=8.0,
            choices=list(HACKRF_SAMP_RATES_MSPS),
            help="HackRF sample rate in Msps (default: 8.0)",
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
        center_freq=options.frequency * 1e6,
        samp_rate=options.samp_rate * 1e6,
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
