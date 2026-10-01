from nightcore.lrc import _lrc_format, _lrc_frac_to_ms, _lrc_scale_ms, retime_lrc


def test_lrc_frac_to_ms():
    assert _lrc_frac_to_ms(None) == 0
    assert _lrc_frac_to_ms("5") == 500
    assert _lrc_frac_to_ms("50") == 500
    assert _lrc_frac_to_ms("500") == 500
    assert _lrc_frac_to_ms("123") == 123


def test_lrc_scale_ms():
    assert _lrc_scale_ms(60000, 1.2) == 50000
    assert _lrc_scale_ms(60000, 1.0) == 60000


def test_lrc_format():
    assert _lrc_format(50000) == "[00:50.00]"
    assert _lrc_format(61000) == "[01:01.00]"
    assert _lrc_format(-5) == "[00:00.00]"


def test_retime_lrc_speed_two():
    text = "[ar:Artist]\n[00:01.00]hello\n[00:03.00]world\n[length:00:04.00]"
    new_text, count = retime_lrc(text, 2.0)
    assert count == 2
    assert "[ar:Artist]" in new_text
    assert "[00:00.50]hello" in new_text
    assert "[00:01.50]world" in new_text
    assert "[length:00:02.00]" in new_text


def test_retime_lrc_speed_one_keeps_counts():
    text = "[00:01.00]hello"
    new_text, count = retime_lrc(text, 1.0)
    assert count == 1
    assert "[00:01.00]hello" in new_text
