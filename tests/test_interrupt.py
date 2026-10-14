"""Two-stage Ctrl+C: finish-then-stop, then abort without tracebacks."""

import signal
import sys

import pytest

from nightcore import util
from nightcore.cli import _stop_early, main


@pytest.fixture(autouse=True)
def _clean_interrupt_state():
    yield
    util._stop_requested = False
    util._aborted = False
    util._active_proc = None
    util._active_outputs.clear()


def test_first_press_flags_stop_without_raising(capsys):
    util._on_sigint(signal.SIGINT, None)  # must not raise
    assert util._stop_requested is True
    assert util._aborted is False
    assert "finishing the current file" in capsys.readouterr().err


def test_second_press_aborts_quietly(capsys):
    util._on_sigint(signal.SIGINT, None)
    with pytest.raises(SystemExit) as exc:
        util._on_sigint(signal.SIGINT, None)
    assert exc.value.code == 130
    assert util._aborted is True
    # No traceback, no debris on either stream from the handler itself.
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


def test_spamming_is_press_two_idempotent(capsys):
    # Presses 3+ land mid-unwind: same SystemExit(130), no tracebacks,
    # flags stable, note printed exactly once.
    util._on_sigint(signal.SIGINT, None)
    for _ in range(4):
        with pytest.raises(SystemExit) as exc:
            util._on_sigint(signal.SIGINT, None)
        assert exc.value.code == 130
    assert util._aborted is True
    err = capsys.readouterr().err
    assert err.count("finishing the current file") == 1
    assert "Traceback" not in err


def test_kill_helper_never_raises():
    util._active_proc = None
    util._kill_active_tool()  # nothing tracked: no-op

    class _Proc:
        pid = 2 ** 40  # no such group: kill fails, swallowed

    util._active_proc = _Proc()
    util._kill_active_tool()


def test_exit_cleanup_deletes_partials(capsys, tmp_path):
    doomed = tmp_path / "partial.flac"
    doomed.write_bytes(b"half")
    kept = tmp_path / "done.flac"
    kept.write_bytes(b"whole")
    util._active_outputs.add(doomed)
    util._aborted = True
    util._exit_cleanup()
    assert not doomed.exists()
    assert kept.exists()  # untracked: untouched
    assert "partial file(s) deleted" in capsys.readouterr().err


def test_exit_cleanup_quiet_without_abort(capsys, tmp_path):
    doomed = tmp_path / "partial.flac"
    doomed.write_bytes(b"half")
    util._active_outputs.add(doomed)
    util._exit_cleanup()  # _aborted False: no-op
    assert doomed.exists()
    assert capsys.readouterr().err == ""


def test_stop_early_tallies_and_exits_130(capsys):
    ctx = {"converted": 2}
    rc = _stop_early(ctx, ["a", "b", "c"], [], 2)
    assert rc == 130
    captured = capsys.readouterr()
    assert "Stopped after 2 of 3" in captured.err
    assert "2 converted" in captured.out


def test_batch_loop_stops_between_files(monkeypatch, capsys, tmp_path):
    import nightcore.cli as cli_mod
    for name in ("a.flac", "b.flac"):
        (tmp_path / name).write_bytes(b"fake")
    calls = []
    monkeypatch.setattr(cli_mod, "run_conversion",
                        lambda *a, **k: calls.append(a[1]) or 0)
    util._stop_requested = True  # as if Ctrl+C landed mid-batch
    monkeypatch.setattr(sys, "argv", ["nightcore", "--batch", str(tmp_path)])
    assert main() == 130
    assert calls == []  # nothing new started
    assert "Stopped after 0 of 2" in capsys.readouterr().err
