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
  power, with live gain controls). Also writes an `adsb_diagnostics_*.npz`
  diagnostic recording with short IQ snapshots around every threshold
  crossing.
- [`decode_adsb_capture.py`](decode_adsb_capture.py) — offline decoder that
  reads those diagnostic NPZ files, re-detects each Mode-S preamble, uses
  the leading DF field to route each clip as either a 56-bit short reply
  (DF0/4/5/11) or a 112-bit long extended-squitter frame (DF17/18/20/21…),
  applies bounded CRC-guided bit correction where the CRC can be
  independently verified, cross-references address-parity replies against
  ICAOs of CRC-verified frames in the same capture, and hands every
  recovered message to [pyModeS](https://github.com/junzis/pyModeS) for
  full ADS-B / Mode-S field decoding. Emits both a terminal summary and a
  JSON report next to the capture.

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
> lobe, and the 2 us envelope smoothing preserves most of the ~1 us pulse
> structure, but there is still no bit-level Mode S recovery here. Use
> `dump1090` for actual decoding.

The four views (no demodulation, no audio):

- **Spectrum plot** — 4 MHz slice centered on 1090 MHz.
- **Waterfall** — spectrum over time, so intermittent bursts remain visible
  after they end.
- **Channel envelope** — magnitude of the channelized IQ (`complex_to_mag`),
  smoothed with an 8-sample boxcar and decimated to 125 ksps, displayed
  over a 1 ms window with an auto rising-edge trigger at level 0.05.
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

## Offline ADS-B decoder

`decode_adsb_capture.py` turns the `adsb_diagnostics_*.npz` files written by
`hackrf_976_antenna.py` into decoded ADS-B / Mode-S messages. For every
triggered 150 us IQ clip it re-detects the four-pulse Mode-S preamble,
slices the 1 Mbps pulse-position-modulated payload, inspects the leading
5-bit downlink format (DF) field to pick 56- or 112-bit message length,
runs a length-appropriate 24-bit CRC (with bounded 0–2 bit syndrome
correction for DFs whose CRC is not XORed with an aircraft address), and
passes every recovered hex message through
[pyModeS](https://github.com/junzis/pyModeS) for full field decoding.

Two downlink-format families are handled separately:

- **Zero-XOR frames** — DF11 (all-call reply, II=0), DF17 (ADS-B extended
  squitter) and DF18 (TIS-B ES). The CRC remainder equals zero when the
  bits are correct, so a CRC-clean message is independently verifiable and
  a 1- or 2-bit syndrome correction is safe to apply.
- **Address-parity frames** — DF0/4/5 (short surveillance replies), DF16
  (long ACAS), DF20/21 (Comm-B) and DF24 (Comm-D). The CRC is XORed with
  the aircraft's 24-bit ICAO address, so pyModeS *derives* the ICAO from
  the CRC XOR-parity but the bit vector cannot be verified from itself.
  These messages are labelled `address_parity` in the report and their
  pyModeS-derived ICAO is cross-referenced against every ICAO recovered
  from a CRC-verified frame in the same capture — matches are promoted to
  `icao_matched`, so reply traffic from an aircraft you also received an
  ADS-B ES from is clearly distinguished from noise-triggered "replies."

### Installing the decoder dependencies

pyModeS is not part of GNU Radio. Install it into the same interpreter you
use to run the flowgraphs so a single Python environment can capture and
decode:

```bash
$GR_PYTHON -m pip install -r decoder_requirements.txt
```

Only NumPy (already required by GNU Radio) and pyModeS >= 3.6 are needed.

### Running the decoder

With no arguments the tool automatically opens the most recent
`adsb_diagnostics_*.npz` file in the current directory:

```bash
$GR_PYTHON decode_adsb_capture.py
```

Explicit filenames and options are all optional:

```bash
$GR_PYTHON decode_adsb_capture.py adsb_diagnostics_20260715_204127.npz \
    --output my_report.json
```

Flags:

- `capture` — optional path to a specific NPZ file. If omitted, the newest
  `adsb_diagnostics_*.npz` in `--directory` (current working directory by
  default) is decoded.
- `--output`, `-o` — path for the JSON report. Defaults to
  `<capture>.decoded.json` alongside the input.
- `--no-json` — skip writing the JSON report; print the summary only.
- `--quiet` — suppress the human-readable summary; still writes JSON.
- `--max-correctable-bits {0,1,2}` — cap on bit-flips the CRC syndrome
  table may apply, for zero-XOR DFs only. Default is `2`. Any correction
  is additionally rejected unless the recovered message still looks like a
  zero-XOR frame (DF11 for short messages, DF17 or DF18 for long messages,
  with a non-zero ICAO), so 2-bit corrections cannot silently invent
  packets from noise. Address-parity DFs are never syndrome-corrected
  because their CRC is XORed with the unknown ICAO.
- `--min-preamble-ratio` / `--min-preamble-pulse-ratio` /
  `--min-bit-confidence` — thresholds used to decide whether a trigger clip
  contains a Mode-S preamble worth reporting.

### Interpreting the output

Every strong preamble candidate becomes one row of the table:

| Column | Meaning |
|---|---|
| `idx` | Trigger clip index inside the NPZ file. |
| `time_s` | Capture time, seconds after recording start. |
| `DF` | Downlink format extracted from the leading 5 bits (0/4/5/11 → short reply, 17/18 → ADS-B ES, 20/21 → Comm-B, …). |
| `bits` | Message length in bits (56 for short replies, 112 for long frames). |
| `status` | One of `valid` (CRC = 0), `corrected_1bit`, `corrected_2bit`, `icao_matched` (address-parity DF whose CRC-XOR ICAO matches a CRC-verified aircraft from the same capture), `address_parity` (address-parity DF with no cross-reference match), or `unverified` (no bounded correction produced a plausible zero-XOR message). |
| `hex` | 14- or 28-character message hex — corrected if a fix was applied, otherwise the raw slicing. |
| `fields` | Highlights from the pyModeS decode (ICAO, type code, callsign, altitude, CPR position, velocity, flight status…) for every non-`unverified` row. Address-parity rows show the CRC-XOR-derived ICAO explicitly — remember it may be wrong if the payload bits were corrupted. |

The JSON report contains everything above plus the full pyModeS decoded
dict for each candidate, the set of ICAOs recovered from CRC-verified
frames, capture metadata, and the decoder settings used, which makes it
easy to compare captures against each other.

Nothing is fabricated for unverified candidates — they are listed so you
can see how many Mode-S bursts the recording actually contained even when
signal quality was too poor to recover the payload. Address-parity rows
whose ICAO does not match any CRC-verified aircraft are similarly
suspect: with an all-noise payload the CRC-XOR still produces *some*
24-bit ICAO, so treat those rows as raw-bits-plus-a-guess rather than
confirmed aircraft data.

### Running the tests

```bash
$GR_PYTHON -m unittest -v test_decode_adsb_capture.py
```

The suite covers CRC arithmetic for both 56- and 112-bit frames, 1- and
2-bit syndrome correction for each length, DF-based message-length
routing, preamble detection, PPM slicing (including short-frame clips),
address-parity classification, ICAO cross-referencing across a capture,
latest-file selection, JSON serialization, and an end-to-end pyModeS
decode against a known-good ADS-B position packet.

