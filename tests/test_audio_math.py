import math

import pytest

from nightcore.audio import (
    bits_from_stream,
    format_rate,
    next_standard_rate,
    semitones_from_speed,
    speed_from_semitones,
    wav_codec_for,
)


def test_semitones_speed_roundtrip():
    for speed in (0.8, 0.9, 1.0, 1.1, 1.2, 1.25, 1.5, 2.0):
        assert speed_from_semitones(semitones_from_speed(speed)) == pytest.approx(speed)


def test_semitones_known_values():
    assert semitones_from_speed(2.0) == pytest.approx(12.0)
    assert semitones_from_speed(1.0) == pytest.approx(0.0)
    assert semitones_from_speed(1.2) == pytest.approx(12 * math.log2(1.2))
    assert speed_from_semitones(12.0) == pytest.approx(2.0)


def test_next_standard_rate():
    assert next_standard_rate(44100) == 44100
    assert next_standard_rate(44101) == 48000
    assert next_standard_rate(52920) == 88200  # 44.1k * 1.2
    assert next_standard_rate(8000) == 8000
    assert next_standard_rate(999999) is None


def test_format_rate():
    assert format_rate(44100) == "44.1kHz"
    assert format_rate(48000) == "48kHz"
    assert format_rate(88200) == "88.2kHz"
    assert format_rate(96000) == "96kHz"


def test_wav_codec_for():
    assert wav_codec_for(None) == "pcm_s16le"
    assert wav_codec_for(8) == "pcm_s16le"
    assert wav_codec_for(16) == "pcm_s16le"
    assert wav_codec_for(24) == "pcm_s24le"
    assert wav_codec_for(32) == "pcm_s32le"


def test_bits_from_stream():
    assert bits_from_stream({"bits_per_raw_sample": 24}) == 24
    assert bits_from_stream({"bits_per_raw_sample": "16"}) == 16
    assert bits_from_stream({"sample_fmt": "s16"}) == 16
    assert bits_from_stream({"sample_fmt": "s32"}) == 32
    assert bits_from_stream({"sample_fmt": "u8"}) == 8
    assert bits_from_stream({"sample_fmt": "fltp"}) is None
    assert bits_from_stream({}) is None
