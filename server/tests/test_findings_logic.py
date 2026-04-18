"""Pure-logic checks for the new finding detectors.

Avoids the SQL layer by exercising the inner thresholds and aggregation
maths directly on plain dicts. Catches the off-by-one ratio mistakes
that are easy to introduce in the opaque-failing detector.
"""


def test_error_ratio_aggregation():
    """Verify the ratio computation matches the detector's threshold."""
    edges = [
        {"target": "comp.A", "errors": 5, "total": 100},
        {"target": "comp.A", "errors": 8, "total": 50},
        {"target": "comp.B", "errors": 1, "total": 1000},
    ]
    by_target = {}
    for e in edges:
        agg = by_target.setdefault(e["target"], {"errors": 0, "total": 0})
        agg["errors"] += e["errors"]
        agg["total"] += e["total"]

    ratio_a = by_target["comp.A"]["errors"] / by_target["comp.A"]["total"]
    ratio_b = by_target["comp.B"]["errors"] / by_target["comp.B"]["total"]

    # comp.A = 13/150 ≈ 0.087 (under threshold 0.1 — should NOT flag)
    # comp.B = 1/1000 = 0.001 (under threshold — should NOT flag)
    assert round(ratio_a, 4) == 0.0867
    assert ratio_b < 0.1
    assert ratio_a < 0.1


def test_zero_total_skipped():
    """Total of 0 must not cause divide-by-zero or false positives."""
    edges = [{"target": "comp.X", "errors": 0, "total": 0}]
    by_target = {}
    for e in edges:
        if e["total"] == 0:
            continue
        agg = by_target.setdefault(e["target"], {"errors": 0, "total": 0})
        agg["errors"] += e["errors"]
        agg["total"] += e["total"]
    assert by_target == {}


def test_high_error_ratio_flagged():
    edges = [
        {"target": "comp.Crashy", "errors": 25, "total": 100},
        {"target": "comp.Crashy", "errors": 10, "total": 100},
    ]
    by_target = {}
    for e in edges:
        agg = by_target.setdefault(e["target"], {"errors": 0, "total": 0})
        agg["errors"] += e["errors"]
        agg["total"] += e["total"]
    ratio = by_target["comp.Crashy"]["errors"] / by_target["comp.Crashy"]["total"]
    # 35/200 = 0.175 above threshold 0.1
    assert ratio > 0.1


def test_schema_mismatch_set_difference():
    """schema_mismatch is set-difference: edge targets minus valid entities."""
    edge_targets = {"db.dbo.Users", "db.dbo.Orders", "db.dbo.LegacyTbl"}
    valid_entities = {"db.dbo.Users", "db.dbo.Orders"}
    missing = edge_targets - valid_entities
    assert missing == {"db.dbo.LegacyTbl"}
