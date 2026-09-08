import json
from pathlib import Path


def test_compiled_state_schema_is_valid_json():
    schema = json.loads(Path("governance/compiled_state_schema.json").read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert "cursor" in schema["required"]
