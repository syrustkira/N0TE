from governance.compile_current import compile_current


def test_repository_current_state_compiles_from_existing_owners():
    current = compile_current(".")
    projection = current["projection"]
    assert projection["cursor"]["job_id"] == "N0TE-CONSTRUCTION-001"
    assert projection["cursor"]["active_object"] == "n0te"
    assert "artist" in projection["retained_scope_refs"]
    assert "n0te" in projection["retained_scope_refs"]
    assert "capability_evidence" in projection["required_functions"]
    assert current["snapshot_id"].startswith("compiled:N0TE-CONSTRUCTION-001:")
