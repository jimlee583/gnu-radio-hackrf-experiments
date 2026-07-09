# GNU Radio HackRF Experiments

A GNU Radio 3.10 + PyQt5 flowgraph that receives broadcast FM from a HackRF One,
demodulates it, and plays the audio through the default system output. A live
waterfall of the tuned spectrum is shown alongside a frequency spinbox for
tuning stations in the 88–108 MHz band.

## Prerequisites

- GNU Radio 3.10 with the `analog`, `audio`, `blocks`, `filter`, `qtgui`, and
  `soapy` modules
- SoapySDR with the HackRF driver
- **PortAudio** — required by GNU Radio's `audio.sink` for playback

On macOS:

```bash
brew install portaudio
```

If the `audio` module was not built against PortAudio, reinstall GNU Radio:

```bash
brew reinstall gnuradio
```

Verify the audio module is available:

```bash
python3 -c "from gnuradio import audio; print('audio OK')"
```

## Usage

Plug in the HackRF, then run:

```bash
python3 hackrf_fm_radio.py --frequency 101.1
```

Flags:

- `--frequency` — center frequency in MHz. Default: `100.0`.
- `--samp-rate` — HackRF sample rate in Msps. Allowed: `2`, `4`, `8`, `10`.
  Default: `8.0`. Higher rates show a wider slice of the FM band on the
  waterfall (8 Msps ≈ 8 MHz of spectrum around the tuned frequency).

The Qt window has a spinbox above the waterfall for tuning any station
between 88.0 and 108.0 MHz while the flowgraph is running, plus LNA/VGA
gain sliders to trim signal level in real time.

De-emphasis is fixed at 75 µs (US broadcast). The audio path decimates the
raw IQ down to a fixed 200 kHz `quad_rate` through the channel filter, then
to 50 kHz mono through `analog.wfm_rcv`, and finally through a volume trim
before the PortAudio sink. Sample rate and quad rate are decoupled, so
`--samp-rate` only affects the spectrum view and the channel filter — the
demod and audio stages are unchanged.
