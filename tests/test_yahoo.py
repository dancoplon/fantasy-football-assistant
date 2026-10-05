from booth.yahoo import _explain, _has_waiver_source


def test_waiver_source_detection_walks_nested_players():
    txn = {"timestamp": "1", "players": {"0": {"player": [[{"name": "x"}], {"transaction_data": [{"source_type": "waivers"}]}]}}}
    assert _has_waiver_source(txn)
    assert not _has_waiver_source({"players": {"0": {"transaction_data": {"source_type": "freeagents"}}}})


def test_explain_flags_unprovisioned_api():
    assert "provisioned" in str(_explain(RuntimeError("401 Client Error: Forbidden")))


def test_explain_flags_yahoo_not_authorized_message():
    # The exact error Dan's first `booth check` returned on Oct 5 2026.
    msg = '{"error": {"yahoo:uri": "/fantasy/v2/game/nfl?format=json", "description": "This application is not authorized to perform this action."}}'
    assert "provisioned" in str(_explain(RuntimeError(msg)))
