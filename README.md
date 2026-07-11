# GNU Radio HackRF Experiments

A GNU Radio 3.10 + PyQt5 flowgraph that receives broadcast FM from a HackRF One,
demodulates it, and plays the audio through the default system output. A live
waterfall of the tuned spectrum is shown alongside a frequency spinbox for
tuning stations in the 88–108 MHz band.

## Prerequisites

- GNU Radio 3.10 with the `analog`, `audio`, `blocks`, `digital`, `filter`,
  `qtgui`, and `soapy` modules
- SoapySDR with the HackRF driver
- **PortAudio** — required by GNU Radio's `audio.sink` for playback
- **gr-rds** (out-of-tree) — for the RDS/RBDS metadata decoder

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

### Install gr-rds

`gr-rds` (the `bastibl/gr-rds` module) is not bundled with GNU Radio and must
be built from source against the same GNU Radio installation you use to run
this script. On macOS with Homebrew GNU Radio, install it into the GNU Radio
venv prefix so its Python bindings are importable:

```bash
brew install cmake pybind11 boost
git clone --branch maint-3.10 https://github.com/bastibl/gr-rds.git
cd gr-rds && mkdir build && cd build
cmake -DCMAKE_INSTALL_PREFIX=/opt/homebrew/opt/gnuradio/libexec/venv \
      -Dpybind11_DIR=/opt/homebrew/opt/pybind11/share/cmake/pybind11 \
      ..
make -j$(sysctl -n hw.ncpu)
make install
```

Verify:

```bash
/opt/homebrew/opt/gnuradio/libexec/venv/bin/python3 -c "import rds; print('gr-rds OK')"
```

On Linux, use the standard `cmake .. && make && sudo make install && sudo
ldconfig` sequence documented in the [gr-rds README](https://github.com/bastibl/gr-rds).

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
gain sliders to trim signal level in real time. Below the waterfall, an RDS
panel shows metadata decoded from the 57 kHz digital subcarrier that most
commercial FM stations broadcast alongside their audio.

De-emphasis is fixed at 75 µs (US broadcast). The audio path decimates the
raw IQ down to a fixed 200 kHz `quad_rate` through the channel filter, then
to 50 kHz mono through `analog.wfm_rcv`, and finally through a volume trim
before the PortAudio sink. Sample rate and quad rate are decoupled, so
`--samp-rate` only affects the spectrum view and the channel filter — the
demod and audio stages are unchanged.

## RDS/RBDS metadata

The receiver decodes the RDS (Europe) / RBDS (US) subcarrier in parallel
with the audio path. The panel shows:

- **Station Name** (PS) — call letters or short branding, e.g. `WXYZ-FM`
- **Program Type** (PTY) — genre category like `Rock`, `News`, `Classical`
- **PI** — 16-bit program identifier code (hex)
- **Radiotext** (RT) — free-form text, typically song title / artist
- **Clock Time** — station-broadcast wall-clock time
- **Alt. Frequencies** — other frequencies the same program is carried on
- Status flags: **TP** (traffic program), **TA** (traffic announcement),
  **Music/Speech**, **Stereo/Mono**, **AH**, **CMP**, **stPTY**

RDS data trickles in slowly (raw bitrate ~1187.5 bps with heavy repetition),
so PS typically appears within a few seconds while RT can take 10–30 s to
settle on a new station. Signal level of the RDS subcarrier varies widely
between stations — a station with clear audio may still have RDS too weak
to decode. Tuning via the spinbox clears the panel automatically so stale
metadata from the previous station is not shown.

The RBDS locale is used by default (`pty_locale=1` when constructing
`rds.parser` in `hackrf_fm_radio.py`); change it to `0` if you are outside
North America and want European PTY category names.
