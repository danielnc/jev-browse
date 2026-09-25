"""Offline checks for scripts/make_demo_gif.py's pure helpers (the recording itself is live)."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("make_demo_gif",
                                              Path(__file__).resolve().parents[1] / "scripts" / "make_demo_gif.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def rec(name, b_wall, passed=True, errors=0):
    clean = {"errors": 0}
    return (Path(name), {"arms": {"B": {"passed": passed, "wall_s": b_wall, "recorder": {"errors": errors}},
                                  "A-fast": {"passed": True, "wall_s": 9, "recorder": clean}}})


def test_speed_fits_the_longest_run_into_the_budget():
    assert demo.speed_for(20) == 1
    assert demo.speed_for(33) == 1.5
    assert demo.speed_for(72.8) == 2.5
    assert demo.speed_for(97.2) == 4


def test_pick_median_uses_the_left_hand_time():
    picked, _ = demo.pick_median([rec("a", 80.0), rec("b", 60.0), rec("c", 70.0)])
    assert picked.name == "c"
    assert demo.pick_median([rec("only", 50.0)])[0].name == "only"


def test_pick_median_refuses_a_failed_attempt():
    with pytest.raises(ValueError, match="did not pass"):
        demo.pick_median([rec("a", 60.0), rec("b", 70.0, passed=False), rec("c", 80.0)])


def test_pick_median_refuses_a_recording_whose_recorder_stalled():
    with pytest.raises(ValueError, match="recorder stalled during B"):
        demo.pick_median([rec("a", 60.0, errors=2)])


def test_recorder_stats(tmp_path):
    f = tmp_path / "frames.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in ({"t": 1, "file": "0.jpg", "capture_ms": 40},
                                                    {"t": 2, "error": "timed out"},
                                                    {"t": 3, "file": "1.jpg", "capture_ms": 90})))
    assert demo.recorder_stats(f) == {"frames": 2, "errors": 1, "capture_ms_median": 90, "capture_ms_max": 90}


def test_stream_timeline_counts_each_assistant_message_once():
    lines = [(0.1, json.dumps({"type": "system", "subtype": "init", "model": "claude-x"})),
             (1.0, json.dumps({"type": "assistant", "message": {"id": "m1"}})),
             (1.2, json.dumps({"type": "assistant", "message": {"id": "m1"}})),
             (3.5, "not json"),
             (4.0, json.dumps({"type": "assistant", "message": {"id": "m2"}}))]
    assert demo.stream_timeline(lines) == ([1.0, 4.0], "claude-x")
