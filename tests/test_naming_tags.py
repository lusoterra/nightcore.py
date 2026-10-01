from pathlib import Path

from nightcore.naming import (
    claim_output_path,
    lossy_suffix_for,
    name_base_for,
    split_shift_tags,
    strip_quality_rate_tags,
)
from nightcore.tags import (
    clean_encoder,
    sanitize_filename,
    simple_title,
    strip_script_tags,
)


def test_split_shift_tags_speed():
    cleaned, speed, st, found, rb = split_shift_tags("song [1.2x]")
    assert (cleaned, speed, st, found, rb) == ("song", 1.2, 0.0, True, None)


def test_split_shift_tags_semitones_and_combo():
    _, speed, st, found, _ = split_shift_tags("song [3.16st]")
    assert (speed, st, found) == (1.0, 3.16, True)
    _, speed, st, found, _ = split_shift_tags("song [1.2x 2.16st]")
    assert (speed, st, found) == (1.2, 2.16, True)


def test_split_shift_tags_keeps_quality_and_titles():
    cleaned, _, _, found, _ = split_shift_tags("song [Greatest Hits] [1.2x]")
    assert cleaned == "song [Greatest Hits]"
    assert found is True
    cleaned, _, _, found, _ = split_shift_tags("song [16bit 44.1kHz]")
    assert cleaned == "song [16bit 44.1kHz]"
    assert found is False


def test_split_shift_tags_reciprocal_product():
    _, speed, _, found, _ = split_shift_tags("test [1.2x] [0.8333333333333334x]")
    assert found is True
    assert abs(speed - 1.0) < 1e-9


def test_strip_quality_rate_tags():
    assert strip_quality_rate_tags("song [16bit 44.1kHz]") == "song"
    assert strip_quality_rate_tags("song [88.2kHz]") == "song"
    assert strip_quality_rate_tags("song [mp3 CBR 320k LAME3.100]") == "song"
    assert strip_quality_rate_tags("song [Greatest Hits]") == "song [Greatest Hits]"
    assert strip_quality_rate_tags("song [1.2x]") == "song [1.2x]"


def test_strip_script_tags():
    assert strip_script_tags("song [1.2x] [88.2kHz]") == "song"
    assert strip_script_tags("song [rb --formant]") == "song"
    assert strip_script_tags("song [Greatest Hits]") == "song [Greatest Hits]"


def test_simple_title():
    assert simple_title("song [1.2x]", "") == "song"
    assert simple_title("song [1.2x]", "nightcored") == "song nightcored"
    assert simple_title("song [Greatest Hits]", "") == "song [Greatest Hits]"


def test_sanitize_filename():
    assert sanitize_filename("a/b:c*d?e") == "a_b_c_d_e"
    assert sanitize_filename("  name. ") == "name"
    assert sanitize_filename("") == "_"


def test_lossy_suffix_for():
    assert lossy_suffix_for("flac", 1000) == ""
    assert lossy_suffix_for("unknowncodec", 1000) == ""
    assert lossy_suffix_for("mp3", 320000, "CBR", "LAME3.100") == " [mp3 CBR 320k LAME3.100]"
    assert lossy_suffix_for("opus", 128000) == " [opus 128k]"


def test_name_base_for():
    assert name_base_for(False, "Artist", "Title", "stem") == "stem"
    assert name_base_for(True, "Artist", "Title", "stem") == "Artist - Title"
    assert name_base_for(True, None, "Title", "stem") == "Title"


def test_claim_output_path(tmp_path):
    ctx = {}
    first = claim_output_path(ctx, tmp_path / "out.flac")
    second = claim_output_path(ctx, tmp_path / "out.flac")
    third = claim_output_path(ctx, tmp_path / "out.flac")
    assert (first.name, second.name, third.name) == ("out.flac", "out (2).flac", "out (3).flac")
    other = claim_output_path(ctx, tmp_path / "sub" / "out.flac")
    assert other == tmp_path / "sub" / "out.flac"


def test_clean_encoder():
    assert clean_encoder("", "flac") is None
    assert clean_encoder("Lavf61.7.100", "flac") is None
    assert clean_encoder("libopus", "opus") is None
    assert clean_encoder("libopus 1.5.2", "opus") == "libopus 1.5.2"
    assert clean_encoder("LAME3.100", "mp3") == "LAME3.100"
