from pathlib import Path


def test_consolidation_branch_has_single_execution_manifest():
    assert Path("governance/execution_manifest.json").is_file()
    assert not Path("governance/execution_manifest_2.json").exists()
