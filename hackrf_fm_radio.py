#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Not titled yet
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from gnuradio import gr
from gnuradio import analog
from gnuradio import audio
from gnuradio import blocks
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import soapy
import math
import sip
import threading



class test1(gr.top_block, Qt.QWidget):

    def __init__(self, center_freq=100.0e6, samp_rate=8e6):
        gr.top_block.__init__(self, "Not titled yet", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Not titled yet")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "test1")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate
        self.quad_rate = quad_rate = 200e3
        self.channel_decim = channel_decim = int(samp_rate / quad_rate)
        self.audio_decimation = audio_decimation = 4
        self.audio_rate = audio_rate = quad_rate / audio_decimation
        self.center_freq = center_freq
        self.volume = volume = 0.3
        self.lna_gain = lna_gain = 16
        self.vga_gain = vga_gain = 16

        ##################################################
        # Blocks
        ##################################################

        self.soapy_hackrf_source_0 = None
        dev = 'driver=hackrf'
        stream_args = ''
        tune_args = ['']
        settings = ['']

        self.soapy_hackrf_source_0 = soapy.source(dev, "fc32", 1, '',
                                  stream_args, tune_args, settings)
        self.soapy_hackrf_source_0.set_sample_rate(0, samp_rate)
        self.soapy_hackrf_source_0.set_bandwidth(0, samp_rate)
        self.soapy_hackrf_source_0.set_frequency(0, center_freq)
        self.soapy_hackrf_source_0.set_gain(0, 'AMP', False)
        self.soapy_hackrf_source_0.set_gain(0, 'LNA', min(max(lna_gain, 0.0), 40.0))
        self.soapy_hackrf_source_0.set_gain(0, 'VGA', min(max(vga_gain, 0.0), 62.0))
        self.qtgui_waterfall_sink_x_0 = qtgui.waterfall_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            center_freq, #fc
            samp_rate, #bw
            "", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_waterfall_sink_x_0.set_update_time(0.10)
        self.qtgui_waterfall_sink_x_0.enable_grid(False)
        self.qtgui_waterfall_sink_x_0.enable_axis_labels(True)



        labels = ['', '', '', '', '',
                  '', '', '', '', '']
        colors = [0, 0, 0, 0, 0,
                  0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
                  1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_waterfall_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_waterfall_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_waterfall_sink_x_0.set_color_map(i, colors[i])
            self.qtgui_waterfall_sink_x_0.set_line_alpha(i, alphas[i])

        self.qtgui_waterfall_sink_x_0.set_intensity_range(-140, 10)

        self._qtgui_waterfall_sink_x_0_win = sip.wrapinstance(self.qtgui_waterfall_sink_x_0.qwidget(), Qt.QWidget)

        self._freq_control_layout = Qt.QHBoxLayout()
        self._freq_label = Qt.QLabel("Frequency:")
        self._freq_spinbox = Qt.QDoubleSpinBox()
        self._freq_spinbox.setDecimals(1)
        self._freq_spinbox.setSingleStep(0.1)
        self._freq_spinbox.setRange(88.0, 108.0)
        self._freq_spinbox.setSuffix(" MHz")
        self._freq_spinbox.setValue(center_freq / 1e6)
        self._freq_spinbox.valueChanged.connect(
            lambda mhz: self.set_center_freq(mhz * 1e6)
        )
        self._freq_control_layout.addWidget(self._freq_label)
        self._freq_control_layout.addWidget(self._freq_spinbox)

        self._lna_label = Qt.QLabel("LNA:")
        self._lna_slider = Qt.QSlider(Qt.Qt.Horizontal)
        self._lna_slider.setRange(0, 40)
        self._lna_slider.setSingleStep(8)
        self._lna_slider.setPageStep(8)
        self._lna_slider.setTickInterval(8)
        self._lna_slider.setTickPosition(Qt.QSlider.TicksBelow)
        self._lna_slider.setValue(int(lna_gain))
        self._lna_value_label = Qt.QLabel(f"{int(lna_gain)} dB")
        self._lna_value_label.setMinimumWidth(48)
        self._lna_slider.valueChanged.connect(self.set_lna_gain)
        self._freq_control_layout.addWidget(self._lna_label)
        self._freq_control_layout.addWidget(self._lna_slider)
        self._freq_control_layout.addWidget(self._lna_value_label)

        self._vga_label = Qt.QLabel("VGA:")
        self._vga_slider = Qt.QSlider(Qt.Qt.Horizontal)
        self._vga_slider.setRange(0, 62)
        self._vga_slider.setSingleStep(2)
        self._vga_slider.setPageStep(2)
        self._vga_slider.setTickInterval(10)
        self._vga_slider.setTickPosition(Qt.QSlider.TicksBelow)
        self._vga_slider.setValue(int(vga_gain))
        self._vga_value_label = Qt.QLabel(f"{int(vga_gain)} dB")
        self._vga_value_label.setMinimumWidth(48)
        self._vga_slider.valueChanged.connect(self.set_vga_gain)
        self._freq_control_layout.addWidget(self._vga_label)
        self._freq_control_layout.addWidget(self._vga_slider)
        self._freq_control_layout.addWidget(self._vga_value_label)

        self._freq_control_layout.addStretch(1)
        self.top_layout.addLayout(self._freq_control_layout)

        self.top_layout.addWidget(self._qtgui_waterfall_sink_x_0_win)

        channel_taps = firdes.low_pass(
            1.0, samp_rate, 90e3, 50e3,
            window.WIN_HAMMING, 6.76
        )
        self.freq_xlating_fir_filter = filter.freq_xlating_fir_filter_ccc(
            channel_decim, channel_taps, 0, samp_rate
        )
        self.wfm_rcv = analog.wfm_rcv(
            quad_rate=quad_rate,
            audio_decimation=audio_decimation,
        )
        self.audio_gain = blocks.multiply_const_ff(volume)
        self.audio_sink = audio.sink(int(audio_rate), "", True)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.soapy_hackrf_source_0, 0), (self.qtgui_waterfall_sink_x_0, 0))
        self.connect((self.soapy_hackrf_source_0, 0), (self.freq_xlating_fir_filter, 0))
        self.connect((self.freq_xlating_fir_filter, 0), (self.wfm_rcv, 0))
        self.connect((self.wfm_rcv, 0), (self.audio_gain, 0))
        self.connect((self.audio_gain, 0), (self.audio_sink, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "test1")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.soapy_hackrf_source_0.set_sample_rate(0, self.samp_rate)
        self.soapy_hackrf_source_0.set_bandwidth(0, self.samp_rate)
        self.qtgui_waterfall_sink_x_0.set_frequency_range(self.center_freq, self.samp_rate)

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        self.soapy_hackrf_source_0.set_frequency(0, self.center_freq)
        self.qtgui_waterfall_sink_x_0.set_frequency_range(self.center_freq, self.samp_rate)

    def get_lna_gain(self):
        return self.lna_gain

    def set_lna_gain(self, lna_gain):
        self.lna_gain = lna_gain
        self.soapy_hackrf_source_0.set_gain(0, 'LNA', min(max(lna_gain, 0.0), 40.0))
        self._lna_value_label.setText(f"{int(lna_gain)} dB")

    def get_vga_gain(self):
        return self.vga_gain

    def set_vga_gain(self, vga_gain):
        self.vga_gain = vga_gain
        self.soapy_hackrf_source_0.set_gain(0, 'VGA', min(max(vga_gain, 0.0), 62.0))
        self._vga_value_label.setText(f"{int(vga_gain)} dB")




def main(top_block_cls=test1, options=None):
    if options is None:
        parser = ArgumentParser()
        parser.add_argument(
            "--frequency", type=float, default=100.0,
            help="Center frequency in MHz (default: 100.0)"
        )
        parser.add_argument(
            "--samp-rate", type=float, default=8.0,
            choices=[2.0, 4.0, 8.0, 10.0],
            help="HackRF sample rate in Msps (default: 8.0)"
        )
        options = parser.parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(
        center_freq=options.frequency * 1e6,
        samp_rate=options.samp_rate * 1e6,
    )

    tb.start()
    tb.flowgraph_started.set()

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

if __name__ == '__main__':
    main()
