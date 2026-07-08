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

`--frequency` is in MHz and defaults to `100.1`. The Qt window has a spinbox
above the waterfall for tuning any station between 88.0 and 108.0 MHz while
the flowgraph is running.

De-emphasis is fixed at 75 µs (US broadcast). The audio path decimates
2 Msps IQ down to 50 kHz mono through a channel filter, `analog.wfm_rcv`, and
a volume trim before the PortAudio sink.
