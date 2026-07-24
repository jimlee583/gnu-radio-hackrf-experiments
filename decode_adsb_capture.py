#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# Offline ADS-B / Mode-S decoder for HackRF diagnostic NPZ captures produced
# by hackrf_976_antenna.py. For every triggered 150 us IQ clip the tool:
#
#   1. Locates the four-pulse Mode-S preamble at 4 Msps.
#   2. Slices 112 pulse-position-modulated bits from the payload window.
#   3. Uses the leading 5-bit downlink format (DF) to decide whether the
#      message is a 56-bit short reply (DF < 16, e.g. DF0/4/5/11) or a
#      112-bit long extended-squitter frame (DF >= 16, e.g. DF17/18/20/21).
#   4. Runs a length-appropriate 24-bit CRC. Zero-XOR DFs (DF11 with II=0,
#      DF17, DF18) can be independently verified as CRC=0, with optional
#      bounded 1- and 2-bit syndrome correction. Address-parity DFs (DF0,
#      DF4, DF5, DF16, DF20, DF21, DF24) cannot be verified from bits
#      alone; their ICAO is derived from the CRC XOR by pyModeS.
#   5. Feeds the recovered hex message to pyModeS for full field decoding.
#   6. Cross-references address-parity frames against ICAOs of independently
#      verified frames in the same capture. Matching frames are promoted
#      to "icao_matched" so the summary distinguishes reply traffic from a
#      known aircraft versus reply traffic whose ICAO is unverifiable.
#   7. Optionally plots |IQ| for every stored trigger clip (--plot).

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np


# Mode-S CRC generator polynomial (25 bits with the implicit leading 1),
# G(x) = x^24 + x^23 + x^22 + x^21 + x^20 + x^19 + x^18 + x^17 + x^16 +
# x^15 + x^14 + x^13 + x^12 + x^10 + x^3 + 1 -> 0x1FFF409.
_CRC_POLY_BITS = np.array(
    [int(c) for c in f"{0x1FFF409:025b}"],
    dtype=np.uint8,
)

# ADS-B messages arrive at 1 Mbps with the four preamble pulses positioned
# at 0, 1, 3.5 and 4.5 us. Every timing constant is expressed in samples
# at CHANNEL_RATE (4 Msps) so pulse pairs land on consecutive samples.
_SAMPLES_PER_BIT = 4
_LONG_MESSAGE_BITS = 112
_SHORT_MESSAGE_BITS = 56
_PPM_PAYLOAD_SAMPLES = _SAMPLES_PER_BIT * _LONG_MESSAGE_BITS
_PREAMBLE_LEN_SAMPLES = 32
_PREAMBLE_PULSE_OFFSETS = (0, 4, 14, 18)
# Sample offsets within the 32-sample preamble that should be quiet between
# and after the four preamble pulses. Used as the "low" reference power.
_PREAMBLE_QUIET_OFFSETS = np.r_[
    np.arange(2, 4),
    np.arange(6, 14),
    np.arange(16, 18),
    np.arange(20, 32),
]

# DF sets used throughout the decoder.
#
# Zero-XOR: the CRC-XOR mask is 0 so a correct message has remainder = 0
# and 1- to 2-bit corrections are meaningful. DF11 nominally XORs its CRC
# with the interrogator ID (II); the vast majority of received DF11 replies
# are unsolicited squitters with II=0 which also produce remainder = 0.
_ZERO_XOR_DFS = frozenset({11, 17, 18})

# Address/parity DFs: CRC is XORed with the 24-bit aircraft address, so
# pyModeS derives ICAO from CRC-XOR-parity and the bits themselves cannot
# be independently verified.
_ADDRESS_PARITY_DFS = frozenset({0, 4, 5, 16, 20, 21, 24, 25, 26, 27, 28, 29, 30, 31})


@dataclass
class DecoderSettings:
    """Runtime knobs for preamble detection and bit correction."""

    preamble_search_start: int = 60
    preamble_search_stop: int = 101
    min_preamble_ratio: float = 4.0
    min_preamble_pulse_ratio: float = 2.5
    min_bit_confidence: float = 0.35
    max_correctable_bits: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "preamble_search_start": self.preamble_search_start,
            "preamble_search_stop": self.preamble_search_stop,
            "min_preamble_ratio": self.min_preamble_ratio,
            "min_preamble_pulse_ratio": self.min_preamble_pulse_ratio,
            "min_bit_confidence": self.min_bit_confidence,
            "max_correctable_bits": self.max_correctable_bits,
        }


@dataclass
class PreambleMatch:
    """Best preamble alignment for a single trigger clip."""

    start_sample: int
    score: float
    ratio: float
    min_pulse_ratio: float
    mean_bit_confidence: float
    bits: np.ndarray
    bit_confidence: np.ndarray


@dataclass
class Candidate:
    """A recovered Mode-S candidate for one trigger clip."""

    clip_index: int
    capture_time_s: float
    preamble_offset_samples: int
    preamble_score: float
    preamble_ratio: float
    preamble_min_pulse_ratio: float
    mean_bit_confidence: float
    raw_hex: str
    correction_status: str
    df: int
    message_bits: int
    corrected_hex: str | None = None
    corrected_bit_positions: list[int] = field(default_factory=list)
    decoded: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_index": self.clip_index,
            "capture_time_s": self.capture_time_s,
            "preamble_offset_samples": self.preamble_offset_samples,
            "preamble_score": self.preamble_score,
            "preamble_ratio": self.preamble_ratio,
            "preamble_min_pulse_ratio": self.preamble_min_pulse_ratio,
            "mean_bit_confidence": self.mean_bit_confidence,
            "raw_hex": self.raw_hex,
            "correction_status": self.correction_status,
            "df": self.df,
            "message_bits": self.message_bits,
            "corrected_hex": self.corrected_hex,
            "corrected_bit_positions": list(self.corrected_bit_positions),
            "decoded": self.decoded,
        }


# ---------------------------------------------------------------------------
# CRC helpers
# ---------------------------------------------------------------------------


def crc_remainder(bits: np.ndarray) -> int:
    """Return the 24-bit Mode-S CRC remainder for a 56- or 112-bit vector.

    A remainder of zero indicates a CRC-valid zero-XOR message (DF11 with
    II=0, DF17, DF18). For address-parity DFs the "remainder" equals the
    aircraft's ICAO address (which is how pyModeS recovers it).
    """

    work = np.asarray(bits, dtype=np.uint8).copy()
    if work.size not in (_SHORT_MESSAGE_BITS, _LONG_MESSAGE_BITS):
        raise ValueError(
            "Expected 56 or 112 bits for CRC, got "
            f"{work.size}"
        )
    for i in range(work.size - 24):
        if work[i]:
            work[i:i + 25] ^= _CRC_POLY_BITS
    packed = 0
    for bit in work[-24:]:
        packed = (packed << 1) | int(bit)
    return packed


def _build_syndrome_tables(
    length: int,
) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    """Enumerate 1- and 2-bit error syndromes for a given message length."""

    single = np.zeros(length, dtype=np.int64)
    zero = np.zeros(length, dtype=np.uint8)
    for i in range(length):
        pattern = zero.copy()
        pattern[i] = 1
        single[i] = crc_remainder(pattern)
    one_map = {int(s): i for i, s in enumerate(single)}
    two_map: dict[int, tuple[int, int]] = {}
    for i in range(length):
        for j in range(i + 1, length):
            key = int(single[i]) ^ int(single[j])
            two_map.setdefault(key, (i, j))
    return one_map, two_map


_ONE_BIT_SYNDROMES_LONG, _TWO_BIT_SYNDROMES_LONG = _build_syndrome_tables(
    _LONG_MESSAGE_BITS
)
_ONE_BIT_SYNDROMES_SHORT, _TWO_BIT_SYNDROMES_SHORT = _build_syndrome_tables(
    _SHORT_MESSAGE_BITS
)

# Backwards-compatible aliases: existing tests and callers refer to the
# long-format syndromes by their original names.
_ONE_BIT_SYNDROMES = _ONE_BIT_SYNDROMES_LONG
_TWO_BIT_SYNDROMES = _TWO_BIT_SYNDROMES_LONG


def _syndrome_tables_for(length: int):
    if length == _LONG_MESSAGE_BITS:
        return _ONE_BIT_SYNDROMES_LONG, _TWO_BIT_SYNDROMES_LONG
    if length == _SHORT_MESSAGE_BITS:
        return _ONE_BIT_SYNDROMES_SHORT, _TWO_BIT_SYNDROMES_SHORT
    raise ValueError(f"Unsupported bit length {length}")


def _bits_to_hex(bits: np.ndarray) -> str:
    """Convert a 56- or 112-bit numpy array into an uppercase hex string."""

    if bits.size not in (_SHORT_MESSAGE_BITS, _LONG_MESSAGE_BITS):
        raise ValueError(f"Expected 56 or 112 bits, got {bits.size}")
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:0{bits.size // 4}X}"


def _extract_df(bits: np.ndarray) -> int:
    """Extract the 5-bit downlink format field from bits[0:5]."""

    return (
        (int(bits[0]) << 4)
        | (int(bits[1]) << 3)
        | (int(bits[2]) << 2)
        | (int(bits[3]) << 1)
        | int(bits[4])
    )


def _extract_icao(bits: np.ndarray) -> int:
    """Return the 24-bit ICAO field at bits[8:32] (only meaningful for
    zero-XOR DFs; address-parity DFs carry ICAO in the CRC instead)."""

    icao = 0
    for bit in bits[8:32]:
        icao = (icao << 1) | int(bit)
    return icao


def message_length_for_df(df: int) -> int:
    """Return 56 for short-format DFs, 112 for long-format DFs.

    Downlink formats below 16 are short (56-bit) Mode-S replies. DF16 and
    above are long (112-bit) frames including ADS-B ES (DF17/18) and the
    Comm-B/D reply families.
    """

    return _SHORT_MESSAGE_BITS if df < 16 else _LONG_MESSAGE_BITS


def _looks_like_zero_xor_frame(bits: np.ndarray) -> bool:
    """Sanity check for a post-correction zero-XOR message.

    For short frames (56 bits) the only zero-XOR DF is DF11.
    For long frames (112 bits) the zero-XOR DFs are DF17 and DF18.
    In every case the ICAO field must be non-zero.
    """

    df = _extract_df(bits)
    if bits.size == _SHORT_MESSAGE_BITS:
        if df != 11:
            return False
    elif bits.size == _LONG_MESSAGE_BITS:
        if df not in (17, 18):
            return False
    else:
        return False
    return _extract_icao(bits) != 0


def attempt_bit_correction(
    bits: np.ndarray,
    max_bits: int = 2,
) -> tuple[np.ndarray, str, list[int]]:
    """Try bounded CRC-syndrome correction.

    Returns ``(bits, status, positions)`` where ``status`` is one of:

    - ``"valid"``: the CRC remainder was already zero and the DF/ICAO
      looked like a zero-XOR frame (DF11 short, or DF17/DF18 long).
    - ``"corrected_1bit"`` / ``"corrected_2bit"``: exactly one or two bits
      were flipped and the result satisfies both the zero-remainder and
      DF/ICAO sanity gate.
    - ``"unverified"``: neither the raw bits nor any bounded correction
      produced a plausible zero-XOR message. Address-parity DFs (DF4/5/20
      etc.) always fall through here because their CRC is XORed with the
      unknown ICAO; ``evaluate_clip`` handles that case separately.
    """

    one_syn, two_syn = _syndrome_tables_for(bits.size)
    remainder = crc_remainder(bits)
    if remainder == 0 and _looks_like_zero_xor_frame(bits):
        return bits, "valid", []

    if max_bits >= 1 and remainder in one_syn:
        idx = one_syn[remainder]
        fixed = bits.copy()
        fixed[idx] ^= 1
        if crc_remainder(fixed) == 0 and _looks_like_zero_xor_frame(fixed):
            return fixed, "corrected_1bit", [idx]

    if max_bits >= 2 and remainder in two_syn:
        i, j = two_syn[remainder]
        fixed = bits.copy()
        fixed[i] ^= 1
        fixed[j] ^= 1
        if crc_remainder(fixed) == 0 and _looks_like_zero_xor_frame(fixed):
            return fixed, "corrected_2bit", [i, j]

    return bits, "unverified", []


# ---------------------------------------------------------------------------
# Preamble detection and PPM bit slicing
# ---------------------------------------------------------------------------


def detect_preamble(
    magnitudes: np.ndarray,
    settings: DecoderSettings,
) -> PreambleMatch | None:
    """Locate the best Mode-S preamble alignment in a magnitude vector.

    The input is the linear |IQ| magnitude of a trigger clip sampled at
    4 Msps. Returns ``None`` if the clip is too short to hold a full 120 us
    Mode-S long transmission at any search offset.
    """

    if magnitudes.ndim != 1:
        raise ValueError("magnitudes must be a 1D array")

    power = (magnitudes * magnitudes).astype(np.float64)
    required = _PREAMBLE_LEN_SAMPLES + _PPM_PAYLOAD_SAMPLES
    stop = min(settings.preamble_search_stop, power.size - required + 1)
    if settings.preamble_search_start >= stop:
        return None

    best: PreambleMatch | None = None
    for start in range(settings.preamble_search_start, stop):
        pulses = np.fromiter(
            (
                power[start + off:start + off + 2].mean()
                for off in _PREAMBLE_PULSE_OFFSETS
            ),
            dtype=np.float64,
            count=len(_PREAMBLE_PULSE_OFFSETS),
        )
        low = power[start + _PREAMBLE_QUIET_OFFSETS].mean()
        denom = low + 1e-20
        ratio = float(pulses.mean() / denom)
        min_pulse_ratio = float(pulses.min() / denom)

        payload_start = start + _PREAMBLE_LEN_SAMPLES
        payload = power[payload_start:payload_start + _PPM_PAYLOAD_SAMPLES]
        symbols = payload.reshape(_LONG_MESSAGE_BITS, _SAMPLES_PER_BIT)
        high = symbols[:, :2].sum(axis=1)
        low_half = symbols[:, 2:].sum(axis=1)
        bits = (high > low_half).astype(np.uint8)
        confidence = np.abs(high - low_half) / (high + low_half + 1e-20)

        # Weight preamble strength by the confidence of the leading 56 bits
        # so that a strong preamble followed by noise does not outrank a
        # slightly-weaker preamble followed by a clean payload. 56 bits is
        # also exactly one short-format Mode-S frame, so this metric is
        # meaningful for both short and long messages.
        score = (
            ratio
            * min(min_pulse_ratio, 4.0)
            * (0.5 + float(confidence[:_SHORT_MESSAGE_BITS].mean()))
        )

        if best is None or score > best.score:
            best = PreambleMatch(
                start_sample=int(start),
                score=float(score),
                ratio=ratio,
                min_pulse_ratio=min_pulse_ratio,
                mean_bit_confidence=float(confidence.mean()),
                bits=bits,
                bit_confidence=confidence,
            )

    return best


def _make_candidate(
    match: PreambleMatch,
    clip_index: int,
    capture_time_s: float,
) -> Candidate:
    df_raw = _extract_df(match.bits)
    length = message_length_for_df(df_raw)
    payload_bits = match.bits[:length].copy()
    raw_hex = _bits_to_hex(payload_bits)

    if df_raw in _ADDRESS_PARITY_DFS:
        # CRC XORs with the unknown ICAO; the bit vector cannot be
        # independently verified. pyModeS will still derive fields including
        # the CRC-XOR-implied ICAO which we cross-reference later.
        return Candidate(
            clip_index=clip_index,
            capture_time_s=capture_time_s,
            preamble_offset_samples=match.start_sample,
            preamble_score=match.score,
            preamble_ratio=match.ratio,
            preamble_min_pulse_ratio=match.min_pulse_ratio,
            mean_bit_confidence=match.mean_bit_confidence,
            raw_hex=raw_hex,
            correction_status="address_parity",
            df=df_raw,
            message_bits=length,
        )

    corrected_bits, status, positions = attempt_bit_correction(payload_bits)
    corrected_hex = (
        _bits_to_hex(corrected_bits) if positions else None
    )
    final_df = _extract_df(corrected_bits) if positions else df_raw
    return Candidate(
        clip_index=clip_index,
        capture_time_s=capture_time_s,
        preamble_offset_samples=match.start_sample,
        preamble_score=match.score,
        preamble_ratio=match.ratio,
        preamble_min_pulse_ratio=match.min_pulse_ratio,
        mean_bit_confidence=match.mean_bit_confidence,
        raw_hex=raw_hex,
        correction_status=status,
        df=final_df,
        message_bits=length,
        corrected_hex=corrected_hex,
        corrected_bit_positions=list(positions),
    )


def evaluate_clip(
    iq_clip: np.ndarray,
    clip_index: int,
    capture_time_s: float,
    settings: DecoderSettings,
) -> Candidate | None:
    """Turn a single trigger clip into a Candidate, or None if too weak."""

    magnitudes = np.abs(iq_clip).astype(np.float64)
    match = detect_preamble(magnitudes, settings)
    if match is None:
        return None
    if (
        match.ratio < settings.min_preamble_ratio
        or match.min_pulse_ratio < settings.min_preamble_pulse_ratio
        or match.mean_bit_confidence < settings.min_bit_confidence
    ):
        return None

    # When correction is disabled the candidate factory still short-circuits
    # to unverified rather than attempting the syndrome tables.
    if settings.max_correctable_bits == 0:
        df_raw = _extract_df(match.bits)
        length = message_length_for_df(df_raw)
        payload_bits = match.bits[:length].copy()
        raw_hex = _bits_to_hex(payload_bits)
        if df_raw in _ADDRESS_PARITY_DFS:
            status = "address_parity"
        else:
            remainder = crc_remainder(payload_bits)
            if remainder == 0 and _looks_like_zero_xor_frame(payload_bits):
                status = "valid"
            else:
                status = "unverified"
        return Candidate(
            clip_index=clip_index,
            capture_time_s=capture_time_s,
            preamble_offset_samples=match.start_sample,
            preamble_score=match.score,
            preamble_ratio=match.ratio,
            preamble_min_pulse_ratio=match.min_pulse_ratio,
            mean_bit_confidence=match.mean_bit_confidence,
            raw_hex=raw_hex,
            correction_status=status,
            df=df_raw,
            message_bits=length,
        )

    return _make_candidate(match, clip_index, capture_time_s)


# ---------------------------------------------------------------------------
# NPZ loading, cross-referencing and CLI
# ---------------------------------------------------------------------------


def pick_latest_capture(directory: Path) -> Path:
    """Return the most recently modified adsb_diagnostics_*.npz file."""

    matches = sorted(
        directory.glob("adsb_diagnostics_*.npz"),
        key=lambda p: p.stat().st_mtime,
    )
    if not matches:
        raise FileNotFoundError(
            f"No adsb_diagnostics_*.npz files found in {directory}"
        )
    return matches[-1]


def _to_json_safe(value: Any) -> Any:
    """Recursively convert pyModeS Decoded objects and numpy scalars to JSON."""

    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    return str(value)


def _pymodes_or_none():
    try:
        import pyModeS  # type: ignore

        return pyModeS
    except ImportError:  # pragma: no cover - dependency guard
        return None


_AUTO_DETECT_PYMODES = object()

# Status labels a candidate may end up with after full decoding.
_STATUS_LABELS = (
    "valid",
    "corrected_1bit",
    "corrected_2bit",
    "icao_matched",
    "address_parity",
    "unverified",
)

# Status labels for which pyModeS should be given a chance to decode.
_STATUSES_WITH_PYMODES = frozenset(
    {"valid", "corrected_1bit", "corrected_2bit", "address_parity", "icao_matched"}
)


def _icao_key(icao_field: Any) -> str | None:
    """Normalize a pyModeS ICAO field to an uppercase hex string, or None."""

    if icao_field is None:
        return None
    text = str(icao_field).strip().upper()
    if not text:
        return None
    return text


def decode_capture(
    npz_path: Path,
    settings: DecoderSettings | None = None,
    pymodes_module: Any = _AUTO_DETECT_PYMODES,
) -> dict[str, Any]:
    """Decode every strong candidate in a diagnostic NPZ file.

    ``pymodes_module`` defaults to auto-detection. Pass an already-imported
    module to inject a specific implementation, or pass ``None`` to force
    the offline mode where only raw bits are reported.
    """

    settings = settings or DecoderSettings()
    with np.load(npz_path, allow_pickle=False) as archive:
        metadata = json.loads(str(archive["metadata_json"]))
        trigger_iq = np.asarray(archive["trigger_iq"], dtype=np.complex64)
        trigger_indices = np.asarray(
            archive["trigger_sample_indices"], dtype=np.int64
        )

    sample_rate = float(metadata.get("channel_sample_rate_sps", 4.0e6))

    candidates: list[Candidate] = []
    for i, iq_clip in enumerate(trigger_iq):
        capture_time = float(trigger_indices[i]) / sample_rate
        candidate = evaluate_clip(
            iq_clip, clip_index=i, capture_time_s=capture_time, settings=settings
        )
        if candidate is not None:
            candidates.append(candidate)

    if pymodes_module is _AUTO_DETECT_PYMODES:
        pymodes_module = _pymodes_or_none()

    if pymodes_module is not None:
        for candidate in candidates:
            if candidate.correction_status not in _STATUSES_WITH_PYMODES:
                continue
            hex_msg = candidate.corrected_hex or candidate.raw_hex
            try:
                decoded = pymodes_module.decode(hex_msg)
            except Exception as exc:  # noqa: BLE001 - decoder should be resilient
                candidate.decoded = {"error": f"{type(exc).__name__}: {exc}"}
            else:
                candidate.decoded = _to_json_safe(decoded)

    # Cross-reference: promote address-parity messages whose CRC-XOR-derived
    # ICAO matches an aircraft we've already CRC-verified in this capture.
    known_icaos: set[str] = set()
    for candidate in candidates:
        if candidate.correction_status not in (
            "valid",
            "corrected_1bit",
            "corrected_2bit",
        ):
            continue
        if isinstance(candidate.decoded, dict):
            key = _icao_key(candidate.decoded.get("icao"))
            if key:
                known_icaos.add(key)

    for candidate in candidates:
        if candidate.correction_status != "address_parity":
            continue
        if not isinstance(candidate.decoded, dict):
            continue
        key = _icao_key(candidate.decoded.get("icao"))
        if key and key in known_icaos:
            candidate.correction_status = "icao_matched"

    counts = {label: 0 for label in _STATUS_LABELS}
    for candidate in candidates:
        counts[candidate.correction_status] = (
            counts.get(candidate.correction_status, 0) + 1
        )

    return {
        "capture_file": str(npz_path),
        "capture_metadata": metadata,
        "decoder_settings": settings.to_dict(),
        "pymodes_available": pymodes_module is not None,
        "known_icaos": sorted(known_icaos),
        "counts": counts,
        "candidate_count": len(candidates),
        "candidates": [c.to_dict() for c in candidates],
    }


def render_summary(report: dict[str, Any]) -> str:
    """Format a decoded-capture report for human consumption."""

    lines: list[str] = []
    md = report["capture_metadata"]
    counts = report["counts"]
    lines.append(f"Capture:        {report['capture_file']}")
    lines.append(f"Recorded at:    {md.get('created_at', 'unknown')}")
    lines.append(
        "Duration:       "
        f"{md.get('duration_s', 0.0):.2f} s "
        f"({md.get('trigger_capture_count', 0)} trigger clips)"
    )
    lines.append(
        "Candidates:     "
        f"{report['candidate_count']} strong preambles"
    )
    lines.append(
        "                "
        f"valid={counts.get('valid', 0)}  "
        f"1-bit fix={counts.get('corrected_1bit', 0)}  "
        f"2-bit fix={counts.get('corrected_2bit', 0)}  "
        f"icao-matched={counts.get('icao_matched', 0)}  "
        f"address-parity={counts.get('address_parity', 0)}  "
        f"unverified={counts.get('unverified', 0)}"
    )
    known = report.get("known_icaos") or []
    if known:
        lines.append(f"Verified ICAOs: {', '.join(known)}")
    if not report["pymodes_available"]:
        lines.append(
            "Note:           pyModeS not installed - only raw bits are reported."
        )

    lines.append("")
    header = (
        f"{'idx':>4}  {'time_s':>10}  {'DF':>3}  {'bits':>4}  "
        f"{'status':<15}  {'hex':<28}  fields"
    )
    lines.append(header)
    lines.append("-" * len(header))

    for candidate in report["candidates"]:
        hex_msg = candidate.get("corrected_hex") or candidate["raw_hex"]
        status = candidate["correction_status"]
        fields = _summarize_decoded(candidate.get("decoded"))
        if status == "unverified":
            fields = "(CRC did not pass; raw bits only)"
        elif status == "address_parity" and not fields.strip():
            fields = "(address-parity DF; ICAO unverified against capture)"
        lines.append(
            f"{candidate['clip_index']:>4}  "
            f"{candidate['capture_time_s']:>10.4f}  "
            f"{candidate['df']:>3}  "
            f"{candidate['message_bits']:>4}  "
            f"{status:<15}  "
            f"{hex_msg:<28}  {fields}"
        )
    return "\n".join(lines)


_INTERESTING_FIELDS = (
    "icao",
    "typecode",
    "callsign",
    "category",
    "altitude",
    "altitude_baro",
    "altitude_gnss",
    "surveillance_status",
    "cpr_format",
    "cpr_lat",
    "cpr_lon",
    "velocity",
    "groundspeed",
    "track",
    "heading",
    "vertical_rate",
    "emergency",
    "squawk",
    "version",
    "capability",
    "capability_text",
    "flight_status",
    "flight_status_text",
)


def _summarize_decoded(decoded: Any) -> str:
    if not decoded:
        return "(no pyModeS decode)"
    if isinstance(decoded, dict) and "error" in decoded and len(decoded) == 1:
        return decoded["error"]
    if not isinstance(decoded, dict):
        return str(decoded)
    parts: list[str] = []
    for field_name in _INTERESTING_FIELDS:
        if field_name in decoded and decoded[field_name] not in (None, ""):
            parts.append(f"{field_name}={decoded[field_name]}")
    if not parts:
        # Fall back to the raw dict for messages that use unusual keys.
        parts = [f"{k}={v}" for k, v in decoded.items() if v not in (None, "")]
    return " ".join(parts[:8])


def default_output_path(input_path: Path) -> Path:
    return input_path.with_suffix(".decoded.json")


def default_plot_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_magnitude.png")


def _matplotlib_or_raise():
    """Import matplotlib, or raise a helpful error if it is missing."""

    try:
        import matplotlib

        return matplotlib
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "error: matplotlib is required for --plot. Install it with:\n"
            "  python3 -m pip install -r decoder_requirements.txt"
        ) from exc


def load_iq_magnitude_arrays(
    npz_path: Path,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Load trigger/baseline IQ magnitudes and capture metadata from an NPZ."""

    with np.load(npz_path, allow_pickle=False) as archive:
        metadata = json.loads(str(archive["metadata_json"]))
        trigger_iq = np.asarray(archive["trigger_iq"], dtype=np.complex64)
        baseline_iq = np.asarray(archive["baseline_iq"], dtype=np.complex64)

    trigger_mag = np.abs(trigger_iq).astype(np.float64)
    baseline_mag = np.abs(baseline_iq).astype(np.float64)
    return trigger_mag, baseline_mag, metadata


def plot_iq_magnitudes(
    npz_path: Path,
    *,
    output_path: Path | None = None,
    show: bool = True,
    include_baseline: bool = False,
):
    """Plot |IQ| for every stored complex sample in a diagnostic capture.

    The NPZ does not keep the full continuous IQ stream — only short clips
    around each trigger (and optional baseline samples). This figure shows:

    - a heatmap of every trigger clip (rows = clip index, columns = sample)
    - a line plot of those same magnitudes concatenated in clip order

    When ``include_baseline`` is true and baseline clips exist, a second
    heatmap panel is added for them.
    """

    matplotlib = _matplotlib_or_raise()
    if not show:
        matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    trigger_mag, baseline_mag, metadata = load_iq_magnitude_arrays(npz_path)
    if trigger_mag.size == 0:
        raise ValueError(f"No trigger IQ samples found in {npz_path}")

    sample_rate = float(metadata.get("channel_sample_rate_sps", 4.0e6))
    clip_samples = int(trigger_mag.shape[1])
    clip_us = clip_samples / sample_rate * 1e6
    trigger_level = None
    settings = metadata.get("final_settings") or {}
    if isinstance(settings, dict) and "trigger_level" in settings:
        trigger_level = float(settings["trigger_level"])

    show_baseline = include_baseline and baseline_mag.size > 0
    nrows = 3 if show_baseline else 2
    fig, axes = plt.subplots(
        nrows,
        1,
        figsize=(12, 3.2 * nrows),
        constrained_layout=True,
        gridspec_kw={"height_ratios": ([1.2, 1.0, 1.2] if show_baseline else [1.4, 1.0])},
    )
    if nrows == 2:
        ax_heat, ax_line = axes
        ax_base = None
    else:
        ax_heat, ax_line, ax_base = axes

    extent = [0.0, clip_us, trigger_mag.shape[0], 0.0]
    image = ax_heat.imshow(
        trigger_mag,
        aspect="auto",
        interpolation="nearest",
        cmap="viridis",
        extent=extent,
    )
    fig.colorbar(image, ax=ax_heat, label="|IQ| amplitude")
    ax_heat.set_title(
        f"Trigger IQ magnitude — {trigger_mag.shape[0]} clips × "
        f"{clip_samples} samples ({npz_path.name})"
    )
    ax_heat.set_xlabel("Time within clip (µs)")
    ax_heat.set_ylabel("Trigger clip index")

    flat = trigger_mag.reshape(-1)
    sample_index = np.arange(flat.size)
    ax_line.plot(sample_index, flat, color="#1f77b4", linewidth=0.4)
    if trigger_level is not None:
        ax_line.axhline(
            trigger_level,
            color="#d62728",
            linestyle="--",
            linewidth=1.0,
            label=f"trigger level {trigger_level:g}",
        )
        ax_line.legend(loc="upper right")
    # Light vertical guides at clip boundaries so the concatenated trace
    # remains readable when zooming.
    if trigger_mag.shape[0] <= 250:
        for boundary in range(clip_samples, flat.size, clip_samples):
            ax_line.axvline(boundary, color="#cccccc", linewidth=0.3, zorder=0)
    ax_line.set_xlim(0, max(flat.size - 1, 1))
    ax_line.set_ylim(bottom=0.0)
    ax_line.set_title("All trigger IQ magnitudes (clips concatenated)")
    ax_line.set_xlabel("Sample index across all trigger clips")
    ax_line.set_ylabel("|IQ| amplitude")
    ax_line.grid(True, alpha=0.25)

    if ax_base is not None:
        base_extent = [0.0, clip_us, baseline_mag.shape[0], 0.0]
        base_image = ax_base.imshow(
            baseline_mag,
            aspect="auto",
            interpolation="nearest",
            cmap="viridis",
            extent=base_extent,
        )
        fig.colorbar(base_image, ax=ax_base, label="|IQ| amplitude")
        ax_base.set_title(
            f"Baseline IQ magnitude — {baseline_mag.shape[0]} clips × "
            f"{baseline_mag.shape[1]} samples"
        )
        ax_base.set_xlabel("Time within clip (µs)")
        ax_base.set_ylabel("Baseline clip index")

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, dpi=150)
    if show:
        plt.show()
    else:
        plt.close(fig)
    return fig


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Decode ADS-B / Mode-S candidates from a HackRF diagnostic "
            "NPZ capture produced by hackrf_976_antenna.py."
        )
    )
    parser.add_argument(
        "capture",
        nargs="?",
        type=Path,
        help=(
            "Path to an adsb_diagnostics_*.npz file. When omitted, the "
            "newest matching file in the current directory is used."
        ),
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help=(
            "JSON report destination. Defaults to <capture>.decoded.json "
            "next to the input file."
        ),
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path.cwd(),
        help=(
            "Directory to scan when no capture argument is given "
            "(default: current directory)."
        ),
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Do not write a JSON report; only print the summary to stdout.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable summary (JSON report is still written).",
    )
    parser.add_argument(
        "--max-correctable-bits",
        type=int,
        default=DecoderSettings.max_correctable_bits,
        choices=(0, 1, 2),
        help="Maximum number of bit errors the CRC syndrome table may fix.",
    )
    parser.add_argument(
        "--min-preamble-ratio",
        type=float,
        default=DecoderSettings.min_preamble_ratio,
        help="Minimum mean-pulse to quiet-power ratio to accept a preamble.",
    )
    parser.add_argument(
        "--min-preamble-pulse-ratio",
        type=float,
        default=DecoderSettings.min_preamble_pulse_ratio,
        help="Minimum single-pulse to quiet-power ratio to accept a preamble.",
    )
    parser.add_argument(
        "--min-bit-confidence",
        type=float,
        default=DecoderSettings.min_bit_confidence,
        help="Minimum mean per-bit PPM confidence to accept a preamble.",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help=(
            "Plot |IQ| for every stored trigger clip (heatmap + concatenated "
            "line). Requires matplotlib."
        ),
    )
    parser.add_argument(
        "--plot-output",
        type=Path,
        help=(
            "Save the magnitude plot to this path (PNG/PDF/…). Implies "
            "--plot. Defaults to <capture>_magnitude.png when --plot is set "
            "without an explicit path and --plot-no-show is used."
        ),
    )
    parser.add_argument(
        "--plot-no-show",
        action="store_true",
        help="Do not open an interactive plot window (useful with --plot-output).",
    )
    parser.add_argument(
        "--plot-baseline",
        action="store_true",
        help="Also include baseline IQ clips in the magnitude plot.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.capture is None:
        try:
            capture_path = pick_latest_capture(args.directory)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        capture_path = args.capture
    if not capture_path.exists():
        print(f"error: capture not found: {capture_path}", file=sys.stderr)
        return 2

    settings = DecoderSettings(
        min_preamble_ratio=args.min_preamble_ratio,
        min_preamble_pulse_ratio=args.min_preamble_pulse_ratio,
        min_bit_confidence=args.min_bit_confidence,
        max_correctable_bits=args.max_correctable_bits,
    )

    report = decode_capture(capture_path, settings=settings)

    if not args.quiet:
        print(render_summary(report))

    if not args.no_json:
        output_path = args.output or default_output_path(capture_path)
        output_path.write_text(json.dumps(report, indent=2) + "\n")
        if not args.quiet:
            print(f"\nWrote decoded report to {output_path}")

    want_plot = args.plot or args.plot_output is not None
    if want_plot:
        plot_path = args.plot_output
        show_plot = not args.plot_no_show
        if plot_path is None and args.plot_no_show:
            plot_path = default_plot_path(capture_path)
        try:
            plot_iq_magnitudes(
                capture_path,
                output_path=plot_path,
                show=show_plot,
                include_baseline=args.plot_baseline,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if plot_path is not None and not args.quiet:
            print(f"Wrote magnitude plot to {plot_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
