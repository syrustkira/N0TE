from governance.compile_current import compile_current


def test_repository_current_state_compiles_from_existing_owners():
    current = compile_current(".")
    projection = current["projection"]
    assert projection["cursor"]["job_id"] == "SERVICE-ACQUISITION-001"
    assert "artist" in projection["retained_scope_refs"]
    assert "service_acquisition" in projection["required_functions"] or "epistemology" in projection["required_functions"]
    assert current["snapshot_id"].startswith("compiled:SERVICE-ACQUISITION-001:")
