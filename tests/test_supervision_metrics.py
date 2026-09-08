from governance.supervision_metrics import SupervisionEvent, summarize_supervision


def test_supervision_tax_counts_only_attention_repair_seconds():
    summary = summarize_supervision([
        SupervisionEvent("USER_REPEATED_CONTEXT", "job-1", 120),
        SupervisionEvent("FUNCTION_DISPATCH_FAILURE", "job-1", 5),
        SupervisionEvent("EXECUTION_REWORK", "job-1", 60),
    ])
    assert summary["USER_REPEATED_CONTEXT"] == 1
    assert summary["FUNCTION_DISPATCH_FAILURE"] == 1
    assert summary["HUMAN_RECONSTRUCTION_SECONDS"] == 180
