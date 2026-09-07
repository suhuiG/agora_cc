import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
sys.path.insert(0, os.path.dirname(__file__))


def test_aggregate_merges_map_results():
    import aggregate_handler as h
    event = {"scan_id": "s1", "results": [
        {"risk": "none", "findings": [], "scanned_areas": ["secret"]},
        {"risk": "high", "findings": [{"code": "SAST_X", "severity": "high"}], "scanned_areas": ["sast"]},
    ]}
    out = h.handler(event, None)
    assert out["scan_id"] == "s1"
    assert out["risk"] == "high"
    assert len(out["findings"]) == 1
    assert set(out["scanned_areas"]) == {"secret", "sast"}


def test_aggregate_tolerates_missing_scanned_areas():
    import aggregate_handler as h
    out = h.handler({"scan_id": "s2", "results": [
        {"risk": "high", "findings": [{"code": "SCANNER_ERROR", "severity": "high"}]}]}, None)
    assert out["risk"] == "high"
    assert out["scanned_areas"] == []


def test_aggregate_writes_to_dynamo(monkeypatch):
    import aggregate_handler as ah

    puts = []

    class _FakeTable:
        def put_item(self, Item):
            puts.append(Item)

    class _FakeResource:
        def Table(self, name):
            return _FakeTable()

    monkeypatch.setenv("AGORA_GOV_TABLE", "AgoraGov-test")
    monkeypatch.setattr(ah, "_ddb_resource", lambda region=None: _FakeResource())

    event = {
        "scan_id": "recX-20260721",
        "record_id": "recX",
        "results": [
            {"risk": "high", "findings": [{"code": "X", "severity": "high"}],
             "scanned_areas": ["secret"]},
        ],
    }
    out = ah.handler(event, None)
    # 기존 반환 계약 불변
    assert out["risk"] == "high"
    # DynamoDB write 발생
    assert len(puts) == 1
    item = puts[0]
    assert item["PK"] == "SCAN#recX"
    assert item["data"]["status"] == "done"
    assert item["data"]["risk"] == "high"
    # verdict 필드 없음(앱이 계산)
    assert "verdict" not in item["data"]


def test_aggregate_no_dynamo_when_no_table(monkeypatch):
    import aggregate_handler as ah
    monkeypatch.delenv("AGORA_GOV_TABLE", raising=False)
    called = []
    monkeypatch.setattr(ah, "_ddb_resource", lambda region=None: called.append(1))
    out = ah.handler({"scan_id": "s", "results": []}, None)
    assert out["risk"] == "none"
    assert called == []  # 테이블 없으면 write 시도 안 함(하위호환)
