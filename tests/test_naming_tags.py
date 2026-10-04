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
    from nightcore.naming import strip_denightcore_tags, strip_empty_display_tags
    assert strip_denightcore_tags("song [denightcored BP] [1.2x]") == "song [1.2x]"
    assert strip_denightcore_tags("song [denightcored NBP]") == "song"
    assert strip_denightcore_tags("song [Greatest Hits]") == "song [Greatest Hits]"
    assert strip_empty_display_tags("song [Nightcore ]") == "song"
    assert strip_empty_display_tags("song [Nightcore]") == "song"
    assert strip_empty_display_tags("song [Nightcore 1.2x]") == "song [Nightcore 1.2x]"
    assert strip_empty_display_tags(
        "song [Nightcore's Mix]") == "song [Nightcore's Mix]"


def test_clean_source_title():
    from nightcore.naming import clean_source_title
    assert clean_source_title(
        "Song [Nightcore 1.2x] [1.2x] [16bit 44.1kHz]") == "Song"
    assert clean_source_title("Song [denightcored BP]") == "Song"
    assert clean_source_title("Song [Greatest Hits]") == "Song [Greatest Hits]"
    assert clean_source_title(
        "Song [Nightcore's Mix]") == "Song [Nightcore's Mix]"


def test_backfill_stamp_settings():
    from nightcore.tags import backfill_stamp_settings, parse_stamp_shifts
    assert backfill_stamp_settings("song", sample_rate=44100) is None
    settings = backfill_stamp_settings(
        "song [1.2x] [mp3 CBR 320k LAME3.100]", sample_rate=52920,
        bits=16, codec="flac", out_format="flac")
    assert settings is not None
    assert "speed=1.2x" in settings
    assert "semitones=" not in settings
    assert "source-rate=44100Hz" in settings
    assert "tools=ffmpeg=" in settings
    assert "sox=" not in settings and "rubberband=" not in settings
    assert parse_stamp_shifts(settings) == (1.2, 0.0)
    pitch = backfill_stamp_settings(
        "song [2.16st]", sample_rate=52920, bits=16, codec="flac",
        out_format="flac")
    assert pitch is not None and "semitones=2.16st" in pitch
    assert parse_stamp_shifts(pitch) == (1.0, 2.16)


def test_recover_filename_quality():
    from nightcore.naming import recover_filename_quality
    r = recover_filename_quality(
        "Song [1.1x] [mp3 320k LAME3.99r]")
    assert r["suffix"] == " [mp3 320k LAME3.99r]"
    assert r["codec"] == "mp3"
    assert r["mode"] is None
    assert r["bitrate_kbps"] == 320
    assert r["encoder"] == "LAME3.99r"
    assert r["depth"] is None
    r = recover_filename_quality(
        "Kobojsarna - X [1.15x] [mp3 VBR 202k LAME3.97]")
    assert r["suffix"] == " [mp3 VBR 202k LAME3.97]"
    assert (r["codec"], r["mode"], r["bitrate_kbps"],
            r["encoder"]) == ("mp3", "VBR", 202, "LAME3.97")
    r = recover_filename_quality(
        "Master Blaster - Y [1.2x] [opus VBR 164k]")
    assert r["suffix"] == " [opus VBR 164k]"
    assert (r["codec"], r["mode"], r["bitrate_kbps"],
            r["encoder"]) == ("opus", "VBR", 164, None)
    r = recover_filename_quality(
        "Song [1.2x 2.16st] [16bit 44.1kHz] [96kHz]")
    assert r["suffix"] == " [16bit 44.1kHz] [96kHz]"
    assert r["codec"] is None
    assert r["depth"] == 16
    assert r["rate_hz"] == 44100
    assert r["resample_hz"] == 96000
    r = recover_filename_quality("Song [Greatest Hits] [1.2x]")
    assert r["suffix"] == "" and r["codec"] is None
    r = recover_filename_quality("plain song")
    assert r["suffix"] == ""


def test_pipeline_origin_roundtrip():
    from nightcore.tags import build_pipeline, pipeline_origin
    pipe = build_pipeline(
        speed=1.1, out_format="flac", source_codec="mp3",
        source_mode="VBR", source_bitrate="202k",
        source_encoder="LAME3.97")
    origin = pipeline_origin(pipe)
    assert origin["codec"] == "mp3"
    assert origin["mode"] == "VBR"
    assert origin["bitrate"] == "202k"
    assert origin["encoder"] == "LAME3.97"
    assert pipeline_origin("no keys here")["codec"] is None


def test_backfill_prefers_filename_origin():
    from nightcore.tags import backfill_stamp_settings, parse_stamp_shifts
    pipe = backfill_stamp_settings(
        "Song [1.1x] [mp3 320k LAME3.99r]", sample_rate=48510,
        bits=32, codec="flac", out_format="flac",
        origin={"suffix": " [mp3 320k LAME3.99r]", "codec": "mp3",
                "bitrate_kbps": 320, "encoder": "LAME3.99r",
                "depth": None, "rate_hz": None, "resample_hz": None})
    assert pipe is not None
    assert "source-codec=mp3" in pipe
    assert "source-bitrate=320k" in pipe
    assert "source-encoder=LAME3.99r" in pipe
    assert "source-depth=32bit" in pipe  # container depth stays probe-based
    assert parse_stamp_shifts(pipe) == (1.1, 0.0)


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
    cleaned, _, _, found, _ = split_shift_tags("song [Nightcore ] [1.2x]")
    assert cleaned == "song"
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
    assert render_name_template("{title} [Nightcore {diff}]",
                                 {"title": "song", "diff": ""}) == "song"
    assert render_name_template("{title} [Nightcore {diff}]",
                                 {"title": "song", "diff": "1.2x"}) == "song [Nightcore 1.2x]"
    assert render_name_template("{title} [Greatest Hits] [{diff}]",
                                 {"title": "song",
                                  "diff": ""}) == "song [Greatest Hits]"
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


def test_template_title_slot_sees_cleaned_source(tmp_path):
    from nightcore.tags import apply_template_tags, parse_tag_template
    tmpl_file = tmp_path / "t.tmpl"
    tmpl_file.write_text('title="{title} [Nightcore {diff}]"\n',
                         encoding="utf-8")
    tmpl = parse_tag_template(str(tmpl_file))
    (_, _, _, _, original_title,
     verbatim) = apply_template_tags(
         tmpl, tmp_path / "nope.flac", "S [1.25x] [mp3 250k]", "A",
         slots={"diff": "1.25x", "quality": "", "resample": "",
                "denightcore": ""})
    assert verbatim is True
    assert original_title == "S [Nightcore 1.25x]"


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
    from nightcore.tags import round_diff_bare, short_num
    assert short_num(1.2) == "1.2"
    assert short_num(2.0) == "2"
    assert short_num(2.166866948) == "2.17"
    assert short_num(2.5, 0) == "2"
    assert short_num("abc") == "abc"
    assert round_diff_bare("2.166866948st", 2) == "2.17st"
    assert round_diff_bare("1.2x", 2) == "1.2x"
    assert round_diff_bare("1.2x 2.166866948st", 2) == "1.2x 2.17st"
    assert round_diff_bare("-2.166866948st", 2) == "-2.17st"
    assert round_diff_bare("2.5st", 0) == "2st"
    assert round_diff_bare("", 2) == ""


def test_stage_records():
    from nightcore.plan import stage_records
    direct = stage_records(
        [("s", ("direct", "asetrate=44100*1.2"))], sample_rate=44100,
        speed=1.2, use_qaac=False, qaac_dither=None, cli_backend=False,
        is_lossy_output=False, enc_fmt="s16")
    assert direct == {"ffmpeg": ["af=asetrate=44100*1.2"], "sox": [],
                      "rubberband": []}
    resample = stage_records(
        [("s", ("resample", "asetrate=44100*1.2", 96000))],
        sample_rate=44100, speed=1.2, use_qaac=False, qaac_dither=None,
        cli_backend=False, is_lossy_output=False, enc_fmt="s16")
    assert resample["sox"] == ["rate=96000Hz"]
    assert resample["ffmpeg"][-1].startswith("dither=aresample=")
    pitch = stage_records(
        [("s", ("pitch", None, 52920, 3.16, None, []))],
        sample_rate=44100, speed=1.0, use_qaac=False, qaac_dither=None,
        cli_backend=False, is_lossy_output=False, enc_fmt="s16")
    assert pitch["sox"][0] == "rate=52920Hz"
    assert pitch["rubberband"] == ["-3,-p=3.16"]
    assert "gain=auto" in pitch["sox"]


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


def test_fold_album_artist():
    from nightcore.tags import fold_album_artist
    assert fold_album_artist({"album_artist": "V"}) == {"albumartist": "V"}
    assert fold_album_artist({"Album_Artist": "V"}) == {"albumartist": "V"}
    assert fold_album_artist(
        {"album_artist": "Y", "albumartist": "X"}) == {"albumartist": "Y"}
    assert fold_album_artist({"artist": "V"}) == {"artist": "V"}


def test_template_keys_lowercased(tmp_path):
    from nightcore.tags import apply_template_tags, parse_tag_template
    tmpl_file = tmp_path / "t.tmpl"
    tmpl_file.write_text('Artist="A"\nAlbumArtist="B"\nGenre="G"\n',
                         encoding="utf-8")
    tmpl = parse_tag_template(str(tmpl_file))
    (_, ff_extra, _, artist, _,
     _) = apply_template_tags(
         tmpl, tmp_path / "nope.flac", "Song", None, slots={})
    assert artist == "A"
    assert ff_extra["artist"] == "A"
    assert ff_extra["genre"] == "G"
    # movenc alias preserved here; fold_album_artist normalizes downstream.
    assert ff_extra["album_artist"] == "B"


def test_autorename_base_strips_rendered_groups():
    from nightcore.tags import autorename_base
    base = dict(drop_shift=True, drop_rate=True)
    assert autorename_base(
        "Code Red", "18 (English Version) [Nightcore 1.2x 2.0st]",
        "stem", **base) == "Code Red - 18 (English Version)"
    assert autorename_base(
        "A", "S [16bit 44.1kHz]", "stem", **base) == "A - S"
    assert autorename_base(None, "Title", "stem", **base) == "Title"
    assert autorename_base(
        "A", "Song [Greatest Hits]", "stem",
        **base) == "A - Song [Greatest Hits]"
    assert autorename_base("A", "[1.2x]", "stem", **base) == "A - stem"
    assert autorename_base(
        "A", "S [96kHz]", "stem", drop_shift=True,
        drop_rate=True) == "A - S"


def test_autorename_base_keeps_without_replacement():
    from nightcore.tags import autorename_base
    # Speed-1.0 transcode: no fresh shift to replace the inherited one.
    assert autorename_base(
        "A", "S [1.1x]", "stem", drop_shift=False,
        drop_rate=True) == "A - S [1.1x]"
    # No resample: no fresh rate to replace the inherited one.
    assert autorename_base(
        "A", "S [96kHz]", "stem", drop_shift=False,
        drop_rate=False) == "A - S [96kHz]"
    # Fresh shift invalidates the old rate tag along with the shift.
    assert autorename_base(
        "A", "S [96kHz]", "stem", drop_shift=True,
        drop_rate=False) == "A - S"


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


def test_backfill_infers_pitch_tools():
    from nightcore.tags import backfill_stamp_settings
    pipe = backfill_stamp_settings(
        "song [1.2x 2.16st]", sample_rate=52920, bits=16,
        codec="flac", out_format="flac")
    assert pipe is not None
    assert "tools=ffmpeg=" in pipe
    assert ",sox=" in pipe and ",rubberband=" in pipe


def test_augment_pipeline_tools():
    from nightcore.tags import augment_pipeline_tools
    thin = "speed=1.2x semitones=2.16st format=flac tools=ffmpeg=n9.0.2"
    fixed = augment_pipeline_tools(thin)
    assert ",sox=" in fixed and ",rubberband=" in fixed
    assert fixed.startswith("speed=1.2x")
    assert augment_pipeline_tools(fixed) == fixed
    plain = "speed=1.2x format=flac tools=ffmpeg=n9.0.2"
    assert augment_pipeline_tools(plain) == plain
    assert augment_pipeline_tools("") == ""
    legacy = "speed=1.2x semitones=2.16st format=flac"
    assert "tools=ffmpeg=" in augment_pipeline_tools(legacy)


def test_parse_stamp_shifts_roundtrip():
    from nightcore.tags import build_pipeline
    pipe = build_pipeline(speed=1.2, semitones=3.16, pitch_mode=True,
                          tools=[("ffmpeg", "n9.0.2")])
    assert pipe.startswith("NiGhTcOrE ")
    assert parse_stamp_shifts(pipe) == (1.2, 3.16)


def test_parse_stamp_shifts_requires_magic():
    # Pre-magic records (legacy DESCRIPTION form included) are refused:
    # only data this tool marked is acted on.
    assert parse_stamp_shifts(
        "Made with nightcore.py x (y)\nSettings: speed=1.2x") is None
    assert parse_stamp_shifts("speed=1.2x format=flac") is None
    assert parse_stamp_shifts("no settings here") is None


def test_ensure_pipeline_magic():
    from nightcore.tags import ensure_pipeline_magic
    assert ensure_pipeline_magic("") == ""
    assert ensure_pipeline_magic("NiGhTcOrE speed=1.2x") == \
        "NiGhTcOrE speed=1.2x"
    assert ensure_pipeline_magic("speed=1.2x") == "NiGhTcOrE speed=1.2x"


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
