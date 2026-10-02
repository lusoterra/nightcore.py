from pathlib import Path

from nightcore.naming import (
    claim_output_path,
    lossy_suffix_for,
    split_shift_tags,
    strip_denightcore_tags,
    strip_quality_rate_tags,
)
from nightcore.tags import (
    clean_encoder,
    compute_new_title,
    parse_stamp_shifts,
    render_autorename,
    render_name_template,
    sanitize_filename,
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


def test_strip_denightcore_tags():
    from nightcore.naming import strip_denightcore_tags
    assert strip_denightcore_tags("song [denightcored BP] [1.2x]") == "song [1.2x]"
    assert strip_denightcore_tags("song [denightcored NBP]") == "song"
    assert strip_denightcore_tags("song [Greatest Hits]") == "song [Greatest Hits]"


def test_denightcore_verdict():
    from nightcore.plan import denightcore_verdict
    assert denightcore_verdict(0.0, None, False, "flac", "") == "denightcored BP"
    assert denightcore_verdict(3.16, None, False, "flac", "") == "denightcored NBP"
    assert denightcore_verdict(0.0, 96000, False, "flac", "") == "denightcored NBP"
    assert denightcore_verdict(0.0, None, False, "mp3", "") == "denightcored NBP"
    assert denightcore_verdict(
        0.0, None, False, "flac",
        "Made with nightcore x (y)\nSettings: speed=1.1x source-codec=mp3"
    ) == "denightcored NBP"
    assert denightcore_verdict(
        0.0, None, False, "flac",
        "Made with nightcore x (y)\nSettings: speed=1.2x resample=96000Hz"
    ) == "denightcored NBP"


def test_split_shift_tags_nightcore_display():
    cleaned, speed, st, found, rb = split_shift_tags("song [Nightcore 1.2x]")
    assert (cleaned, speed, st, found, rb) == ("song", 1.2, 0.0, True, None)
    cleaned, _, _, found, _ = split_shift_tags("song [Nightcore's Mix] [1.2x]")
    assert cleaned == "song [Nightcore's Mix]"
    assert found is True


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


def test_render_name_template_bare_placeholders():
    assert render_name_template("{title} [{diff}]",
                                 {"title": "song", "diff": "1.2x"}) == "song [1.2x]"
    assert render_name_template("{title} [Nightcore {diff}]",
                                 {"title": "song", "diff": "1.2x"}) == "song [Nightcore 1.2x]"


def test_render_name_template_drops_empty_brackets():
    assert render_name_template("{title} [{diff}]",
                                 {"title": "song", "diff": ""}) == "song"
    assert render_name_template("{title} [{diff}] [{quality}] [{resample}]",
                                 {"title": "song", "diff": "1.2x",
                                  "quality": "", "resample": ""}) == "song [1.2x]"


def test_compute_new_title_default_diff_only():
    assert compute_new_title(None, "song", "song", " [1.2x]",
                             diff="1.2x") == "song [Nightcore 1.2x]"
    assert compute_new_title(None, "song", "song", "",
                             diff="") == "song"
    assert compute_new_title(None, "song", "song", " [2.166866948st]",
                             diff="2.166866948st") == "song [Nightcore 2.17st]"


def test_compute_new_title_verbatim():
    assert compute_new_title(
        None, "song", "song [1.2x] [16bit 44.1kHz]", " [1.2x]",
        diff="1.2x", title_verbatim=True) == "song [1.2x] [16bit 44.1kHz]"


def test_template_title_slots_rendered_verbatim(tmp_path):
    from nightcore.tags import apply_template_tags
    tmpl_file = tmp_path / "t.tmpl"
    tmpl_file.write_text('title="{title} [Nightcore {diff}]"\n',
                         encoding="utf-8")
    from nightcore.tags import parse_tag_template
    tmpl = parse_tag_template(str(tmpl_file))
    (title_base, _, _, artist, original_title,
     verbatim) = apply_template_tags(
         tmpl, tmp_path / "nope.flac", "Song", "Singer",
         slots={"diff": "1.2x", "quality": "16bit 44.1kHz",
                "resample": "", "denightcore": ""})
    assert verbatim is True
    assert original_title == "Song [Nightcore 1.2x]"
    assert artist is None  # no artist= entry, strip mode drops it

    assert compute_new_title(tmpl, title_base, original_title,
                             " [1.2x]", diff="1.2x",
                             title_verbatim=verbatim) == "Song [Nightcore 1.2x]"


def test_template_plain_title_verbatim(tmp_path):
    from nightcore.tags import apply_template_tags, parse_tag_template
    tmpl_file = tmp_path / "t.tmpl"
    tmpl_file.write_text('title="My Mix"\n', encoding="utf-8")
    tmpl = parse_tag_template(str(tmpl_file))
    (title_base, _, _, _, original_title,
     verbatim) = apply_template_tags(
         tmpl, tmp_path / "nope.flac", "Song", "Singer",
         slots={"diff": "1.2x", "quality": "", "resample": "",
                "denightcore": ""})
    assert verbatim is True
    assert original_title == "My Mix"

    assert compute_new_title(tmpl, title_base, original_title,
                             " [1.2x]", diff="1.2x",
                             title_verbatim=verbatim) == "My Mix"


def test_round_diff_bare():
    from nightcore.tags import round_diff_bare
    assert round_diff_bare("2.166866948st", 2) == "2.17st"
    assert round_diff_bare("1.2x", 2) == "1.2x"
    assert round_diff_bare("1.2x 2.166866948st", 2) == "1.2x 2.17st"
    assert round_diff_bare("-2.166866948st", 2) == "-2.17st"
    assert round_diff_bare("2.5st", 0) == "2st"
    assert round_diff_bare("", 2) == ""


def test_render_diff_modifier():
    assert render_name_template("{title} [Nightcore {diff(2)}]",
                                 {"title": "song",
                                  "diff": "2.166866948st"}) == "song [Nightcore 2.17st]"
    assert render_name_template("{title} [{diff(2)}]",
                                 {"title": "song", "diff": ""}) == "song"


def test_template_comment_stays_literal(tmp_path):
    from nightcore.tags import apply_template_tags, parse_tag_template
    tmpl_file = tmp_path / "t.tmpl"
    tmpl_file.write_text('comment="slots like {diff} stay"\n',
                         encoding="utf-8")
    tmpl = parse_tag_template(str(tmpl_file))
    (_, ff_extra, _, _, _,
     _) = apply_template_tags(
         tmpl, tmp_path / "nope.flac", "Song", "Singer",
         slots={"diff": "1.2x", "quality": "", "resample": "",
                "denightcore": ""})
    assert ff_extra["comment"] == "slots like {diff} stay"


def test_render_autorename_modes():
    class Args:
        autorename = None
    assert render_autorename(Args(), "Artist", "Title", "stem") is None

    class Bare:
        autorename = ""
    assert render_autorename(Bare(), "Artist", "Title", "stem") == "Artist - Title"
    assert render_autorename(Bare(), None, "Title", "stem") == "Title"

    class Custom:
        autorename = "{artist} - {title} [{diff}]"
    assert render_autorename(Custom(), "Artist", "Title", "stem",
                             diff="1.2x") == "Artist - Title [1.2x]"


def test_parse_stamp_shifts_roundtrip():
    from nightcore.tags import build_stamp_settings
    settings = build_stamp_settings(speed=1.2, semitones=3.16,
                                    pitch_mode=True)
    assert parse_stamp_shifts("Made with nightcore x (y)\n" + settings) == (1.2, 3.16)


def test_parse_stamp_shifts_rejects_deninghtcore():
    assert parse_stamp_shifts(
        "Made with nightcore x (y)\nSettings: speed=1.2x denightcore=BP"
    ) is None
    assert parse_stamp_shifts("no settings here") is None


def test_sanitize_filename():
    assert sanitize_filename("a/b:c*d?e") == "a_b_c_d_e"
    assert sanitize_filename("  name. ") == "name"
    assert sanitize_filename("") == "_"


def test_lossy_suffix_for():
    assert lossy_suffix_for("flac", 1000) == ""
    assert lossy_suffix_for("unknowncodec", 1000) == ""
    assert lossy_suffix_for("mp3", 320000, "CBR", "LAME3.100") == " [mp3 CBR 320k LAME3.100]"
    assert lossy_suffix_for("opus", 128000) == " [opus 128k]"


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
