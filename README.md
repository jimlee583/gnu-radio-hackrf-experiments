# GNU Radio HackRF Experiments

A collection of GNU Radio 3.10 + PyQt5 flowgraphs driving a HackRF One:

- [`hackrf_fm_radio.py`](hackrf_fm_radio.py) — broadcast FM receiver
  (88–108 MHz) with a waterfall, tuning spinbox, LNA/VGA sliders, and
  RDS/RBDS metadata panel.
- [`hackrf_976_antenna.py`](hackrf_976_antenna.py) — fixed-tune **1090 MHz**
  (Mode S / ADS-B) channelized viewer for antenna evaluation and burst
  spotting. Tunes the HackRF LO to 1091 MHz, then uses a frequency-translating
  FIR filter to digitally shift, low-pass and decimate the 1090 MHz channel
  down to 4 Msps (FFT + waterfall + channel envelope + averaged channel
  power, with live gain controls).

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

### Python interpreter (macOS)

Homebrew GNU Radio installs into its own virtualenv. Use that Python to run
these scripts — the system `python3` does not have `gnuradio`, `PyQt5`, or
SoapySDR:

```bash
/opt/homebrew/opt/gnuradio/libexec/venv/bin/python3 -c "
from gnuradio import gr, qtgui, soapy
from PyQt5 import Qt, sip
print('Environment OK')
"
```

If that prints `Environment OK`, run the flowgraphs with the same interpreter
(shown below as `$GR_PYTHON` for brevity):

```bash
GR_PYTHON=/opt/homebrew/opt/gnuradio/libexec/venv/bin/python3
```

## Usage

Plug in the HackRF, then run:

```bash
$GR_PYTHON hackrf_fm_radio.py --frequency 101.1
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

## 1090 MHz ADS-B channelized viewer

`hackrf_976_antenna.py` is a fixed-tune channelized instrument for looking
at the 1090 MHz band (Mode S / ADS-B) and judging how well a narrow-band
antenna is picking up signal. The signal chain is:

```
HackRF @ 1091 MHz LO, 8 Msps
        |
        v
freq_xlating_fir_filter_ccc: shift -1 MHz -> DC, LPF ~900 kHz, decimate /2
        |
        v
All four displays run at 4 Msps, centered on 1090 MHz
```

Tuning the LO to 1091 MHz keeps the 1090 MHz target off the HackRF's DC/LO
spur; the frequency-translating FIR filter then digitally shifts the
channel of interest back to DC while low-pass filtering and decimating in
one block.

> Note: this tool is for antenna evaluation and activity spotting, not
> ADS-B decoding. The ~900 kHz channel filter passes the full ADS-B main
> lobe, but the 16 us envelope smoothing still throws away the sharp 0.5 us
> pulse structure Mode S decoders need. Use `dump1090` for actual decoding.

The four views (no demodulation, no audio):

- **Spectrum plot** — 4 MHz slice centered on 1090 MHz.
- **Waterfall** — spectrum over time, so intermittent bursts remain visible
  after they end.
- **Channel envelope** — magnitude of the channelized IQ (`complex_to_mag`),
  smoothed with a 64-sample boxcar and decimated to 125 ksps, displayed
  over a 50 ms window with an auto rising-edge trigger at level 0.05.
  ADS-B frames show up as short envelope bumps that latch in the trigger
  against the ~0.04 noise floor. The plot's control panel lets you drag the
  trigger level and y-axis live; the underlying constants (`SMOOTH_N`,
  `SMOOTH_DECIM`, `TIME_WINDOW_S`, `TRIGGER_LEVEL`) are at the top of the
  script for wider changes.
- **Averaged channel power (dBFS)** — one number that responds in real time
  as you rotate or reposition the antenna; the fastest way to A/B compare
  orientations or gain settings.

Run it:

```bash
$GR_PYTHON hackrf_976_antenna.py
# or trim gains from the CLI:
$GR_PYTHON hackrf_976_antenna.py --lna 24 --vga 20 --amp
```

CLI flags (all optional):

- `--lna` — initial IF LNA gain in dB, 0–40 in 8 dB steps. Default: `16`.
- `--vga` — initial baseband VGA gain in dB, 0–62 in 2 dB steps. Default: `16`.
- `--amp` — enable the HackRF front-end RF amplifier (~+14 dB). Off by
  default; useful for very weak signals but easy to overload with.

The tune frequency (1091 MHz LO), channel center (1090 MHz) and sample rates
(8 Msps in, 4 Msps out) are fixed; there is no runtime tuning control.

Live controls in the Qt window:

| Control | HackRF knob | Notes |
|---|---|---|
| **RF AMP** checkbox | Front-end amplifier (`AMP`) | ~+14 dB before the mixer. Boosts weak signals *and* the noise floor. |
| **LNA** slider (0–40 dB) | IF LNA (`LNA`) | Primary gain knob; adjust first. |
| **VGA** slider (0–62 dB) | Baseband VGA (`VGA`) | Fine trim after the LNA. |

### Suggested workflow for antenna evaluation

1. Start with **AMP off**, **LNA 16 dB**, **VGA 16 dB**. Note the noise
   floor on the FFT.
2. Raise **LNA** in 8 dB steps. Both signal peaks *and* the noise floor
   should climb together. If peaks start clipping or the spectrum looks
   "filled in," back off — the front end is overloading.
3. Only enable **AMP** if the target signal is still buried in noise.
   Watch for flat-topped peaks, new spurs, or a suddenly-jumping noise
   floor, which all indicate the amp is being overdriven.
4. With gains set sensibly, rotate/reposition the antenna and watch the
   **Avg Channel Power** number and the height of the 1090 MHz peak in the
   FFT — the difference between orientations is your practical measure of
   antenna performance.
5. Watch the **Channel Envelope** time plot for short amplitude spikes —
   that is burst activity from aircraft transponders on the channel (Mode S
   / ADS-B at 1090 MHz). A flat trace means no traffic or the signal is too
   weak; raise gain or confirm aircraft are nearby.

This tool only needs the base GNU Radio + Soapy/HackRF stack; it does not
depend on PortAudio or gr-rds.
