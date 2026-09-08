from pathlib import Path


def test_stop_marker_exists_for_this_consolidation_pass():
    assert Path("governance/intentional_stop.txt").is_file()
