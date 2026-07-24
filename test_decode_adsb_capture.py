#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# SPDX-License-Identifier: GPL-3.0
#
# Synthetic and end-to-end tests for decode_adsb_capture.py. Runs with the
# standard-library unittest module so no extra test dependencies are needed.

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

import decode_adsb_capture as dc


# The A2990A airborne-position packet that was CRC-verified from the real
# helicopter capture. Using a real ADS-B message means the CRC helper is
# validated against an independently-known-good remainder.
_VALID_HEX = "8DA2990A59A42263CF2970446770"


def _hex_to_bits(hex_msg: str) -> np.ndarray:
    """Return a bit vector for a 14- or 28-character Mode-S hex message."""

    if len(hex_msg) not in (14, 28):
        raise ValueError(
            f"Expected 14 or 28 hex chars, got {len(hex_msg)}"
        )
    length = len(hex_msg) * 4
    value = int(hex_msg, 16)
    bits = np.zeros(length, dtype=np.uint8)
    for i in range(length):
        bits[length - 1 - i] = (value >> i) & 1
    return bits


def _build_df11_short(icao: int = 0xA2990A, capability: int = 5) -> str:
    """Return a synthetic DF11 all-call reply (II=0) as a 14-char hex string."""

    header = (11 << 27) | ((capability & 0x7) << 24) | (icao & 0xFFFFFF)
    header_bits = np.array(
        [(header >> (31 - i)) & 1 for i in range(32)], dtype=np.uint8
    )
    padded = np.concatenate((header_bits, np.zeros(24, dtype=np.uint8)))
    work = padded.copy()
    for i in range(32):
        if work[i]:
            work[i:i + 25] ^= dc._CRC_POLY_BITS
    crc = 0
    for bit in work[-24:]:
        crc = (crc << 1) | int(bit)
    return f"{(header << 24) | crc:014X}"


def _build_df4_short(
    icao: int = 0xA2990A,
    flight_status: int = 0,
    downlink_request: int = 0,
    utility_message: int = 0,
    altitude_code: int = 0,
) -> str:
    """Return a synthetic DF4 short surveillance altitude reply.

    DF4 uses address/parity: the trailing 24 bits are CRC(data) XOR ICAO,
    which is how pyModeS derives the ICAO from the bit vector.
    """

    header = (
        (4 << 27)
        | ((flight_status & 0x7) << 24)
        | ((downlink_request & 0x1F) << 19)
        | ((utility_message & 0x3F) << 13)
        | (altitude_code & 0x1FFF)
    )
    header_bits = np.array(
        [(header >> (31 - i)) & 1 for i in range(32)], dtype=np.uint8
    )
    padded = np.concatenate((header_bits, np.zeros(24, dtype=np.uint8)))
    work = padded.copy()
    for i in range(32):
        if work[i]:
            work[i:i + 25] ^= dc._CRC_POLY_BITS
    crc = 0
    for bit in work[-24:]:
        crc = (crc << 1) | int(bit)
    ap = crc ^ (icao & 0xFFFFFF)
    return f"{(header << 24) | ap:014X}"


def _bits_to_iq_clip(
    bits: np.ndarray,
    preamble_start: int = 80,
    clip_samples: int = 600,
    signal_amplitude: float = 0.5,
    noise_amplitude: float = 0.01,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Build a synthetic 4 Msps Mode-S clip carrying ``bits`` in PPM.

    Works for both 56-bit short replies and 112-bit long frames. Samples
    beyond the transmitted message stay at noise floor.
    """

    rng = rng or np.random.default_rng(1)
    magnitude = rng.normal(loc=0.0, scale=noise_amplitude, size=clip_samples)
    magnitude = np.abs(magnitude).astype(np.float64)

    # Preamble pulses at 0, 1, 3.5, 4.5 us -> sample offsets 0/4/14/18.
    for offset in (0, 4, 14, 18):
        magnitude[preamble_start + offset:preamble_start + offset + 2] += (
            signal_amplitude
        )

    payload_start = preamble_start + 32
    for i, bit in enumerate(bits):
        symbol = payload_start + i * 4
        if bit:
            magnitude[symbol:symbol + 2] += signal_amplitude
        else:
            magnitude[symbol + 2:symbol + 4] += signal_amplitude

    # Convert magnitudes into complex samples with random phase per sample
    # so that np.abs(iq) reproduces the intended envelope.
    phases = rng.uniform(0.0, 2.0 * np.pi, size=clip_samples)
    return (magnitude * np.exp(1j * phases)).astype(np.complex64)


def _write_synthetic_capture(
    tmp_path: Path,
    clips: list[np.ndarray],
    filename: str = "adsb_diagnostics_synthetic.npz",
) -> Path:
    """Write a minimal but schema-compatible diagnostic NPZ file."""

    metadata = {
        "format_version": 1,
        "channel_sample_rate_sps": 4_000_000.0,
        "clip_samples": clips[0].shape[0],
        "pretrigger_us": 20.0,
        "trigger_capture_count": len(clips),
        "duration_s": 1.0,
        "created_at": "test",
    }
    trigger_iq = np.stack(clips).astype(np.complex64)
    trigger_indices = np.arange(len(clips), dtype=np.int64) * 4_000_000
    baseline_iq = np.empty((0, clips[0].shape[0]), dtype=np.complex64)
    baseline_indices = np.empty(0, dtype=np.int64)
    path = tmp_path / filename
    np.savez_compressed(
        path,
        metadata_json=np.array(json.dumps(metadata)),
        summary_columns=np.array(["time_s", "rms_amplitude", "peak_amplitude"]),
        summaries=np.empty((0, 3), dtype=np.float32),
        magnitude_samples=np.empty(0, dtype=np.float32),
        trigger_iq=trigger_iq,
        trigger_sample_indices=trigger_indices,
        baseline_iq=baseline_iq,
        baseline_sample_indices=baseline_indices,
    )
    return path


class CrcTests(unittest.TestCase):
    """CRC arithmetic sanity checks against a known-valid ADS-B packet."""

    def test_valid_message_has_zero_remainder(self):
        bits = _hex_to_bits(_VALID_HEX)
        self.assertEqual(dc.crc_remainder(bits), 0)

    def test_flipping_any_bit_breaks_crc(self):
        bits = _hex_to_bits(_VALID_HEX)
        for position in (0, 5, 47, 87, 111):
            corrupted = bits.copy()
            corrupted[position] ^= 1
            with self.subTest(position=position):
                self.assertNotEqual(dc.crc_remainder(corrupted), 0)

    def test_syndrome_tables_have_expected_sizes(self):
        self.assertEqual(len(dc._ONE_BIT_SYNDROMES), 112)
        # Two-bit syndromes reuse keys for equivalent errors; still large.
        self.assertGreater(len(dc._TWO_BIT_SYNDROMES), 5_000)

    def test_short_frame_crc_is_zero_for_df11(self):
        hex_short = _build_df11_short()
        bits = _hex_to_bits(hex_short)
        self.assertEqual(bits.size, 56)
        self.assertEqual(dc.crc_remainder(bits), 0)

    def test_short_syndrome_tables_have_expected_sizes(self):
        self.assertEqual(len(dc._ONE_BIT_SYNDROMES_SHORT), 56)
        # C(56, 2) = 1540 distinct 2-bit patterns; some hash into the same
        # syndrome but the table should still be at or near that size.
        self.assertGreater(len(dc._TWO_BIT_SYNDROMES_SHORT), 1_400)

    def test_message_length_routing(self):
        for df in (0, 4, 5, 11):
            self.assertEqual(dc.message_length_for_df(df), 56)
        for df in (16, 17, 18, 20, 21, 24):
            self.assertEqual(dc.message_length_for_df(df), 112)


class BitCorrectionTests(unittest.TestCase):
    """CRC-syndrome-based bit-error correction, bounded by sanity checks."""

    def test_no_correction_needed_for_valid_message(self):
        bits = _hex_to_bits(_VALID_HEX)
        corrected, status, positions = dc.attempt_bit_correction(bits)
        self.assertEqual(status, "valid")
        self.assertEqual(positions, [])
        np.testing.assert_array_equal(corrected, bits)

    def test_single_bit_error_is_corrected(self):
        bits = _hex_to_bits(_VALID_HEX)
        corrupted = bits.copy()
        corrupted[42] ^= 1
        corrected, status, positions = dc.attempt_bit_correction(corrupted)
        self.assertEqual(status, "corrected_1bit")
        self.assertEqual(positions, [42])
        np.testing.assert_array_equal(corrected, bits)

    def test_double_bit_error_is_corrected(self):
        bits = _hex_to_bits(_VALID_HEX)
        corrupted = bits.copy()
        corrupted[10] ^= 1
        corrupted[75] ^= 1
        corrected, status, positions = dc.attempt_bit_correction(corrupted)
        self.assertEqual(status, "corrected_2bit")
        self.assertEqual(sorted(positions), [10, 75])
        np.testing.assert_array_equal(corrected, bits)

    def test_random_noise_reports_unverified(self):
        rng = np.random.default_rng(7)
        random_bits = rng.integers(0, 2, size=112, dtype=np.uint8)
        _, status, positions = dc.attempt_bit_correction(random_bits)
        # Occasionally noise still lands on a 2-bit syndrome; when that
        # happens, the ADS-B DF/ICAO sanity check keeps us from lying.
        if status.startswith("corrected"):
            self.assertTrue(positions)
        else:
            self.assertEqual(status, "unverified")

    def test_disabling_correction_returns_unverified(self):
        bits = _hex_to_bits(_VALID_HEX)
        corrupted = bits.copy()
        corrupted[7] ^= 1
        _, status, positions = dc.attempt_bit_correction(
            corrupted, max_bits=0
        )
        self.assertEqual(status, "unverified")
        self.assertEqual(positions, [])

    def test_short_frame_single_bit_error_is_corrected(self):
        bits = _hex_to_bits(_build_df11_short())
        corrupted = bits.copy()
        corrupted[39] ^= 1
        corrected, status, positions = dc.attempt_bit_correction(corrupted)
        self.assertEqual(status, "corrected_1bit")
        self.assertEqual(positions, [39])
        np.testing.assert_array_equal(corrected, bits)

    def test_short_frame_double_bit_error_is_corrected(self):
        bits = _hex_to_bits(_build_df11_short())
        corrupted = bits.copy()
        corrupted[12] ^= 1
        corrupted[47] ^= 1
        corrected, status, positions = dc.attempt_bit_correction(corrupted)
        self.assertEqual(status, "corrected_2bit")
        self.assertEqual(sorted(positions), [12, 47])
        np.testing.assert_array_equal(corrected, bits)

    def test_short_frame_valid_message_reports_valid(self):
        bits = _hex_to_bits(_build_df11_short())
        _, status, positions = dc.attempt_bit_correction(bits)
        self.assertEqual(status, "valid")
        self.assertEqual(positions, [])

    def test_short_frame_correction_rejects_bogus_df(self):
        # Start from a valid DF11 short frame, flip a bit that would move
        # the DF field out of the zero-XOR set. Since the corrected result
        # would no longer look like DF11, correction must reject it and
        # fall back to unverified.
        bits = _hex_to_bits(_build_df11_short())
        # Corrupt two random bits so remainder != 0 but no plausible fix
        # produces DF11 with a valid ICAO.
        rng = np.random.default_rng(3)
        for _ in range(30):
            corrupted = bits.copy()
            positions = rng.choice(56, size=3, replace=False)
            for p in positions:
                corrupted[int(p)] ^= 1
            _, status, _ = dc.attempt_bit_correction(corrupted)
            if status == "unverified":
                break
        else:  # pragma: no cover - safety net
            self.fail("Failed to produce an unverified short frame")


class PreambleAndSlicingTests(unittest.TestCase):
    """Preamble alignment and PPM bit recovery on synthetic waveforms."""

    def test_detects_preamble_at_expected_offset(self):
        bits = _hex_to_bits(_VALID_HEX)
        clip = _bits_to_iq_clip(bits, preamble_start=80)
        magnitudes = np.abs(clip).astype(np.float64)
        match = dc.detect_preamble(magnitudes, dc.DecoderSettings())
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.start_sample, 80)
        np.testing.assert_array_equal(match.bits, bits)

    def test_evaluate_clip_produces_valid_candidate(self):
        bits = _hex_to_bits(_VALID_HEX)
        clip = _bits_to_iq_clip(bits)
        candidate = dc.evaluate_clip(
            clip,
            clip_index=3,
            capture_time_s=12.5,
            settings=dc.DecoderSettings(),
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.correction_status, "valid")
        self.assertEqual(candidate.raw_hex, _VALID_HEX)
        self.assertEqual(candidate.clip_index, 3)
        self.assertAlmostEqual(candidate.capture_time_s, 12.5)

    def test_pure_noise_clip_is_rejected(self):
        rng = np.random.default_rng(42)
        clip = (
            rng.normal(0, 0.005, size=600)
            + 1j * rng.normal(0, 0.005, size=600)
        ).astype(np.complex64)
        candidate = dc.evaluate_clip(
            clip,
            clip_index=0,
            capture_time_s=0.0,
            settings=dc.DecoderSettings(),
        )
        self.assertIsNone(candidate)

    def test_evaluate_clip_recovers_short_df11_frame(self):
        hex_short = _build_df11_short()
        short_bits = _hex_to_bits(hex_short)
        # Pad the bit vector so the 112-bit PPM slicer has samples for the
        # rest of the payload window. The extra bits carry no meaning; the
        # decoder should still slice only the first 56 bits based on DF.
        padded = np.concatenate((short_bits, np.zeros(56, dtype=np.uint8)))
        clip = _bits_to_iq_clip(padded)
        candidate = dc.evaluate_clip(
            clip,
            clip_index=7,
            capture_time_s=1.25,
            settings=dc.DecoderSettings(),
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.correction_status, "valid")
        self.assertEqual(candidate.df, 11)
        self.assertEqual(candidate.message_bits, 56)
        self.assertEqual(candidate.raw_hex, hex_short)

    def test_evaluate_clip_marks_short_df4_as_address_parity(self):
        hex_short = _build_df4_short(icao=0xA2990A, altitude_code=0x1234)
        short_bits = _hex_to_bits(hex_short)
        padded = np.concatenate((short_bits, np.zeros(56, dtype=np.uint8)))
        clip = _bits_to_iq_clip(padded)
        candidate = dc.evaluate_clip(
            clip,
            clip_index=2,
            capture_time_s=0.5,
            settings=dc.DecoderSettings(),
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.correction_status, "address_parity")
        self.assertEqual(candidate.df, 4)
        self.assertEqual(candidate.message_bits, 56)
        self.assertEqual(candidate.raw_hex, hex_short)
        self.assertIsNone(candidate.corrected_hex)


class LatestCaptureTests(unittest.TestCase):
    """Default-file selection when the CLI is invoked without a path."""

    def test_picks_newest_matching_file(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            (tmp / "adsb_diagnostics_20200101_000000.npz").write_bytes(b"a")
            time.sleep(0.01)
            newer = tmp / "adsb_diagnostics_20260716_120000.npz"
            newer.write_bytes(b"b")
            picked = dc.pick_latest_capture(tmp)
            self.assertEqual(picked, newer)

    def test_missing_directory_raises(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(FileNotFoundError):
                dc.pick_latest_capture(Path(raw))


class DecodeCaptureTests(unittest.TestCase):
    """End-to-end run of decode_capture on a synthetic NPZ file."""

    def test_synthetic_capture_produces_valid_candidate(self):
        bits = _hex_to_bits(_VALID_HEX)
        clip = _bits_to_iq_clip(bits)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = _write_synthetic_capture(tmp, [clip])
            report = dc.decode_capture(path, pymodes_module=None)

        self.assertEqual(report["candidate_count"], 1)
        candidate = report["candidates"][0]
        self.assertEqual(candidate["correction_status"], "valid")
        self.assertEqual(candidate["raw_hex"], _VALID_HEX)
        # Without pyModeS installed we still get the raw hex + status.
        self.assertIsNone(candidate["decoded"])
        self.assertEqual(report["counts"]["valid"], 1)

    def test_report_serializes_to_json(self):
        bits = _hex_to_bits(_VALID_HEX)
        clip = _bits_to_iq_clip(bits)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = _write_synthetic_capture(tmp, [clip])
            report = dc.decode_capture(path, pymodes_module=None)
            payload = json.dumps(report)
        # Round-trip should reproduce structure.
        restored = json.loads(payload)
        self.assertEqual(restored["candidates"][0]["raw_hex"], _VALID_HEX)

    def test_pymodes_integration_fills_decoded_fields(self):
        try:
            import pyModeS  # type: ignore
        except ImportError:
            self.skipTest("pyModeS is not installed in this environment")

        bits = _hex_to_bits(_VALID_HEX)
        clip = _bits_to_iq_clip(bits)
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = _write_synthetic_capture(tmp, [clip])
            report = dc.decode_capture(path, pymodes_module=pyModeS)

        decoded = report["candidates"][0]["decoded"]
        self.assertIsNotNone(decoded)
        assert isinstance(decoded, dict)
        self.assertEqual(decoded.get("icao"), "A2990A")
        self.assertTrue(decoded.get("crc_valid", False))
        self.assertEqual(decoded.get("typecode"), 11)

    def test_address_parity_frame_promoted_when_icao_matches(self):
        try:
            import pyModeS  # type: ignore
        except ImportError:
            self.skipTest("pyModeS is not installed in this environment")

        # Long DF17 for A2990A - independently verifiable.
        long_bits = _hex_to_bits(_VALID_HEX)
        long_clip = _bits_to_iq_clip(long_bits)
        # Short DF4 whose CRC-XOR ICAO is also A2990A.
        df4_hex = _build_df4_short(icao=0xA2990A, altitude_code=0x0DEF)
        df4_bits = _hex_to_bits(df4_hex)
        df4_padded = np.concatenate((df4_bits, np.zeros(56, dtype=np.uint8)))
        df4_clip = _bits_to_iq_clip(df4_padded)
        # Short DF4 whose CRC-XOR ICAO is a different aircraft.
        other_hex = _build_df4_short(icao=0xC0FFEE, altitude_code=0x100)
        other_bits = _hex_to_bits(other_hex)
        other_padded = np.concatenate((other_bits, np.zeros(56, dtype=np.uint8)))
        other_clip = _bits_to_iq_clip(other_padded)

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = _write_synthetic_capture(
                tmp, [long_clip, df4_clip, other_clip]
            )
            report = dc.decode_capture(path, pymodes_module=pyModeS)

        by_hex = {c["raw_hex"]: c for c in report["candidates"]}
        self.assertEqual(by_hex[_VALID_HEX]["correction_status"], "valid")
        self.assertEqual(by_hex[df4_hex]["correction_status"], "icao_matched")
        self.assertEqual(
            by_hex[other_hex]["correction_status"], "address_parity"
        )
        self.assertIn("A2990A", report["known_icaos"])
        self.assertEqual(report["counts"]["icao_matched"], 1)
        self.assertEqual(report["counts"]["address_parity"], 1)
        self.assertEqual(report["counts"]["valid"], 1)


class RealCaptureTests(unittest.TestCase):
    """Confirm the recorded helicopter capture decodes as expected."""

    CAPTURE = Path(__file__).parent / "adsb_diagnostics_20260715_204127.npz"

    def setUp(self):
        if not self.CAPTURE.exists():
            self.skipTest(f"{self.CAPTURE.name} not present")

    def test_recorded_capture_recovers_helicopter_position(self):
        report = dc.decode_capture(self.CAPTURE, pymodes_module=None)
        hex_messages = {c["raw_hex"] for c in report["candidates"]}
        self.assertIn(
            "8DA2990A59A42263CF2970446770",
            hex_messages,
            "The known CRC-valid A2990A position packet was not recovered.",
        )
        valid = [
            c for c in report["candidates"] if c["correction_status"] == "valid"
        ]
        self.assertTrue(valid, "Expected at least one CRC-valid candidate")

    def test_recorded_capture_recovers_short_df11_frames(self):
        report = dc.decode_capture(self.CAPTURE, pymodes_module=None)
        short_candidates = [
            c for c in report["candidates"] if c["message_bits"] == 56
        ]
        self.assertTrue(
            short_candidates,
            "Expected at least one 56-bit short-frame candidate",
        )
        recovered = [
            c
            for c in short_candidates
            if c["correction_status"]
            in ("valid", "corrected_1bit", "corrected_2bit")
        ]
        self.assertTrue(
            recovered,
            "Expected at least one CRC-verified DF11 all-call reply",
        )


if __name__ == "__main__":  # pragma: no cover - manual run
    unittest.main()
