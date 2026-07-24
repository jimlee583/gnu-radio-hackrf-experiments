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

import json
import signal
import sys
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path

import numpy as np
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

# Preserve the 0.5 us ADS-B pulse structure in the envelope display. A
# two-sample moving average at 4 Msps smooths noise over 0.5 us without
# decimating away the 1 Mbps PPM waveform. A 150 us window fits a complete
# 120 us extended-squitter frame with a little context around it.
SMOOTH_N = 2
SMOOTH_DECIM = 1
ENV_RATE = CHANNEL_RATE
TIME_WINDOW_S = 0.00015
TIME_SINK_POINTS = int(TIME_WINDOW_S * ENV_RATE)  # 600 pts

# Default just above the measured noise peaks while remaining sensitive to
# the stronger transient activity seen in diagnostic captures.
TRIGGER_LEVEL = 0.015


class adsb_diagnostic_sink(gr.sync_block):
    """Collect compact statistics and short IQ snapshots for troubleshooting."""

    def __init__(
        self,
        output_path,
        sample_rate,
        trigger_level,
        lna_gain,
        vga_gain,
        amp_enabled,
    ):
        gr.sync_block.__init__(
            self,
            name="ADS-B diagnostic recorder",
            in_sig=[np.complex64],
            out_sig=None,
        )
        self.output_path = Path(output_path)
        self.sample_rate = float(sample_rate)
        self.trigger_level = float(trigger_level)
        self.current_settings = {
            "lna_gain_db": int(lna_gain),
            "vga_gain_db": int(vga_gain),
            "rf_amp_enabled": bool(amp_enabled),
            "trigger_level": self.trigger_level,
        }

        self.clip_samples = int(150e-6 * self.sample_rate)
        self.pretrigger_samples = int(20e-6 * self.sample_rate)
        self.summary_samples = int(0.010 * self.sample_rate)
        self.max_trigger_clips = 200
        self.max_baseline_clips = 120
        self.max_magnitude_samples = 2_000_000

        self.total_samples = 0
        self.sum_power = 0.0
        self.peak_amplitude = 0.0
        self._tail = np.empty(0, dtype=np.complex64)
        self._summary_pending = np.empty(0, dtype=np.float32)
        self._summary_window_count = 0
        self._summary_batches = []
        self._magnitude_samples = []
        self._magnitude_sample_count = 0
        self._trigger_clips = []
        self._trigger_indices = []
        self._baseline_clips = []
        self._baseline_indices = []
        self._next_baseline_sample = 0
        self._last_trigger_sample = -self.clip_samples
        self._events = [
            {
                "time_s": 0.0,
                "event": "start",
                **self.current_settings,
            }
        ]
        self._saved = False

    def update_settings(self, **settings):
        self.current_settings.update(settings)
        self._events.append(
            {
                "time_s": self.total_samples / self.sample_rate,
                "event": "settings_changed",
                **self.current_settings,
            }
        )
        if "trigger_level" in settings:
            self.trigger_level = float(settings["trigger_level"])

    def _collect_summaries(self, magnitudes):
        if self._summary_pending.size:
            magnitudes = np.concatenate((self._summary_pending, magnitudes))

        window_count = magnitudes.size // self.summary_samples
        used_count = window_count * self.summary_samples
        if window_count:
            windows = magnitudes[:used_count].reshape(
                window_count, self.summary_samples
            )
            rms = np.sqrt(np.mean(windows * windows, axis=1))
            peaks = np.max(windows, axis=1)
            first_window = self._summary_window_count
            times = (
                np.arange(first_window, first_window + window_count) + 0.5
            ) * (self.summary_samples / self.sample_rate)
            self._summary_batches.append(
                np.column_stack((times, rms, peaks)).astype(np.float32)
            )
            self._summary_window_count += window_count

        self._summary_pending = magnitudes[used_count:].copy()

    def _collect_baseline_clips(self, samples, block_start):
        combined = np.concatenate((self._tail, samples))
        combined_start = block_start - self._tail.size
        combined_end = combined_start + combined.size
        while (
            len(self._baseline_clips) < self.max_baseline_clips
            and self._next_baseline_sample + self.clip_samples <= combined_end
        ):
            if self._next_baseline_sample >= combined_start:
                local_start = self._next_baseline_sample - combined_start
                local_end = local_start + self.clip_samples
                self._baseline_clips.append(
                    combined[local_start:local_end].copy()
                )
                self._baseline_indices.append(self._next_baseline_sample)
            self._next_baseline_sample += int(self.sample_rate)

    def _collect_trigger_clips(self, samples, block_start):
        if len(self._trigger_clips) >= self.max_trigger_clips:
            return

        combined = np.concatenate((self._tail, samples))
        combined_start = block_start - self._tail.size
        magnitudes = np.abs(combined)
        crossings = np.flatnonzero(
            (magnitudes[:-1] < self.trigger_level)
            & (magnitudes[1:] >= self.trigger_level)
        ) + 1

        for crossing in crossings:
            trigger_sample = combined_start + int(crossing)
            clip_start = crossing - self.pretrigger_samples
            clip_end = clip_start + self.clip_samples
            if trigger_sample - self._last_trigger_sample < self.clip_samples:
                continue
            if clip_start < 0 or clip_end > combined.size:
                continue

            self._trigger_clips.append(
                combined[clip_start:clip_end].copy()
            )
            self._trigger_indices.append(trigger_sample)
            self._last_trigger_sample = trigger_sample
            if len(self._trigger_clips) >= self.max_trigger_clips:
                break

        tail_samples = self.clip_samples + self.pretrigger_samples + 1
        self._tail = combined[-tail_samples:].copy()

    def work(self, input_items, output_items):
        samples = np.asarray(input_items[0], dtype=np.complex64)
        if samples.size == 0:
            return 0

        block_start = self.total_samples
        magnitudes = np.abs(samples).astype(np.float32)
        self.total_samples += samples.size
        self.sum_power += float(np.sum(magnitudes * magnitudes, dtype=np.float64))
        self.peak_amplitude = max(
            self.peak_amplitude, float(np.max(magnitudes))
        )

        self._collect_summaries(magnitudes)
        self._collect_baseline_clips(samples, block_start)
        self._collect_trigger_clips(samples, block_start)

        remaining = self.max_magnitude_samples - self._magnitude_sample_count
        if remaining > 0:
            sampled = magnitudes[::256][:remaining].copy()
            self._magnitude_samples.append(sampled)
            self._magnitude_sample_count += sampled.size

        return samples.size

    @staticmethod
    def _stack_clips(clips, clip_samples):
        if not clips:
            return np.empty((0, clip_samples), dtype=np.complex64)
        return np.stack(clips).astype(np.complex64, copy=False)

    def stop(self):
        if self._saved:
            return True
        self._saved = True

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        summaries = (
            np.concatenate(self._summary_batches)
            if self._summary_batches
            else np.empty((0, 3), dtype=np.float32)
        )
        magnitude_samples = (
            np.concatenate(self._magnitude_samples)
            if self._magnitude_samples
            else np.empty(0, dtype=np.float32)
        )
        rms_amplitude = (
            float(np.sqrt(self.sum_power / self.total_samples))
            if self.total_samples
            else 0.0
        )
        percentiles = (
            np.percentile(magnitude_samples, [5, 50, 90, 95, 99, 99.9])
            if magnitude_samples.size
            else np.zeros(6)
        )
        metadata = {
            "format_version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "tune_frequency_hz": TUNE_FREQ,
            "channel_center_hz": CHANNEL_CENTER,
            "source_sample_rate_sps": SAMP_RATE,
            "channel_sample_rate_sps": self.sample_rate,
            "channel_passband_hz": 1.8e6,
            "duration_s": self.total_samples / self.sample_rate,
            "total_samples": self.total_samples,
            "rms_amplitude": rms_amplitude,
            "peak_amplitude": self.peak_amplitude,
            "magnitude_percentiles": {
                label: float(value)
                for label, value in zip(
                    ["p05", "p50", "p90", "p95", "p99", "p99_9"],
                    percentiles,
                )
            },
            "trigger_capture_count": len(self._trigger_clips),
            "baseline_capture_count": len(self._baseline_clips),
            "clip_samples": self.clip_samples,
            "clip_duration_us": self.clip_samples / self.sample_rate * 1e6,
            "pretrigger_us": self.pretrigger_samples / self.sample_rate * 1e6,
            "final_settings": self.current_settings,
            "events": self._events,
        }

        np.savez_compressed(
            self.output_path,
            metadata_json=np.array(json.dumps(metadata)),
            summary_columns=np.array(
                ["time_s", "rms_amplitude", "peak_amplitude"]
            ),
            summaries=summaries,
            magnitude_samples=magnitude_samples,
            trigger_iq=self._stack_clips(
                self._trigger_clips, self.clip_samples
            ),
            trigger_sample_indices=np.asarray(
                self._trigger_indices, dtype=np.int64
            ),
            baseline_iq=self._stack_clips(
                self._baseline_clips, self.clip_samples
            ),
            baseline_sample_indices=np.asarray(
                self._baseline_indices, dtype=np.int64
            ),
        )
        summary_path = self.output_path.with_suffix(".json")
        summary_path.write_text(json.dumps(metadata, indent=2) + "\n")
        print(
            f"Saved ADS-B diagnostics to {self.output_path} and {summary_path}",
            file=sys.stderr,
        )
        return True


class antenna_viewer(gr.top_block, Qt.QWidget):

    def __init__(
        self,
        lna_gain=32,
        vga_gain=24,
        amp_enabled=False,
        diagnostic_path=None,
    ):
        gr.top_block.__init__(self, "HackRF 1090 MHz ADS-B Channel Viewer", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("HackRF 1090 MHz ADS-B Channel Viewer")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme("gnuradio-grc"))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)

        # The combined GNU Radio displays can be taller than a laptop screen.
        # Keep the full widgets (including the time sink's bottom X axis)
        # reachable instead of allowing the window manager to clip them.
        self.top_scroll_layout = Qt.QVBoxLayout(self)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll.setWidgetResizable(True)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)

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
        self.trigger_level = TRIGGER_LEVEL
        if diagnostic_path is None:
            diagnostic_path = datetime.now().strftime(
                "adsb_diagnostics_%Y%m%d_%H%M%S.npz"
            )
        self.diagnostic_path = diagnostic_path

        # Average about 128 us of contiguous channel power, approximately one
        # long ADS-B frame. Decimation is applied after this average so short
        # bursts are not spread across seconds of sparse input samples.
        self._power_avg_len = 512
        # Reduce the already-averaged stream before feeding the number sink.
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
        self.freq_sink.set_fft_average(0.05)
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
            f"1090 MHz Channel Envelope ({TIME_WINDOW_S * 1e6:g} us, smoothed)",
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
            qtgui.TRIG_MODE_NORM,
            qtgui.TRIG_SLOPE_POS,
            self.trigger_level,
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
        self.diagnostic_sink = adsb_diagnostic_sink(
            self.diagnostic_path,
            CHANNEL_RATE,
            self.trigger_level,
            self.lna_gain,
            self.vga_gain,
            self.amp_enabled,
        )

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

        row += 1

        controls.addWidget(Qt.QLabel("Trigger level:"), row, 0)
        self._trigger_level_input = Qt.QDoubleSpinBox()
        self._trigger_level_input.setDecimals(4)
        self._trigger_level_input.setRange(0.0, 1.0)
        self._trigger_level_input.setSingleStep(0.005)
        self._trigger_level_input.setKeyboardTracking(False)
        self._trigger_level_input.setValue(self.trigger_level)
        self._trigger_level_input.valueChanged.connect(self.set_trigger_level)
        controls.addWidget(self._trigger_level_input, row, 1)

        row += 1
        controls.addWidget(Qt.QLabel("Diagnostics:"), row, 0)
        controls.addWidget(Qt.QLabel(str(self.diagnostic_path)), row, 1, 1, 4)

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
        self.connect((self.chan_filter, 0), (self.diagnostic_sink, 0))
        self.connect((self.mag_squared, 0), (self.moving_avg, 0))
        self.connect((self.moving_avg, 0), (self.decim, 0))
        self.connect((self.decim, 0), (self.nlog, 0))
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
        self.diagnostic_sink.update_settings(lna_gain_db=self.lna_gain)

    def set_vga_gain(self, vga_gain):
        self.vga_gain = int(vga_gain)
        self.hackrf_source.set_gain(0, "VGA", min(max(float(self.vga_gain), 0.0), 62.0))
        self._vga_value_label.setText(f"{self.vga_gain} dB")
        self.diagnostic_sink.update_settings(vga_gain_db=self.vga_gain)

    def set_amp(self, enabled):
        self.amp_enabled = bool(enabled)
        self.hackrf_source.set_gain(0, "AMP", self.amp_enabled)
        self.diagnostic_sink.update_settings(
            rf_amp_enabled=self.amp_enabled
        )

    def set_trigger_level(self, trigger_level):
        self.trigger_level = float(trigger_level)
        self.time_sink.set_trigger_mode(
            qtgui.TRIG_MODE_NORM,
            qtgui.TRIG_SLOPE_POS,
            self.trigger_level,
            0,
            0,
            "",
        )
        self.diagnostic_sink.update_settings(
            trigger_level=self.trigger_level
        )


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
            "--lna", type=int, default=32,
            help="Initial LNA (IF) gain in dB, 0-40 step 8 (default: 32)",
        )
        parser.add_argument(
            "--vga", type=int, default=24,
            help="Initial VGA (baseband) gain in dB, 0-62 step 2 (default: 24)",
        )
        parser.add_argument(
            "--amp", action="store_true",
            help="Enable the HackRF front-end RF amplifier (~+14 dB)",
        )
        parser.add_argument(
            "--diagnostics",
            default=datetime.now().strftime(
                "adsb_diagnostics_%Y%m%d_%H%M%S.npz"
            ),
            help=(
                "Output NPZ file for statistics and short IQ captures "
                "(default: timestamped file in the current directory)"
            ),
        )
        options = parser.parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(
        lna_gain=options.lna,
        vga_gain=options.vga,
        amp_enabled=options.amp,
        diagnostic_path=options.diagnostics,
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
