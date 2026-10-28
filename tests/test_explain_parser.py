"""Unit test: explain parser against MongoDB's documented plan shapes (no DB needed)."""
from indexing_demo import parse_explain

IX = {"queryPlanner": {"winningPlan": {"stage": "FETCH", "inputStage": {"stage": "IXSCAN", "indexName": "ix_bus_date"}}},
      "executionStats": {"nReturned": 30, "totalDocsExamined": 30, "totalKeysExamined": 30, "executionTimeMillis": 0}}
CS = {"queryPlanner": {"winningPlan": {"stage": "COLLSCAN", "direction": "forward"}},
      "executionStats": {"nReturned": 150, "totalDocsExamined": 150, "totalKeysExamined": 0, "executionTimeMillis": 1}}
SBE = {"queryPlanner": {"winningPlan": {"queryPlan": {"stage": "FETCH", "inputStage": {"stage": "IXSCAN", "indexName": "ix_route"}},
                                         "slotBasedPlan": {"stages": "..."}}},
       "executionStats": {"nReturned": 60, "totalDocsExamined": 60, "executionTimeMillis": 0}}


def test_ixscan():
    r = parse_explain(IX); assert (r["stage"], r["index"], r["examined"]) == ("IXSCAN", "ix_bus_date", 30)


def test_collscan():
    r = parse_explain(CS); assert (r["stage"], r["examined"]) == ("COLLSCAN", 150)


def test_sbe_variant():
    r = parse_explain(SBE); assert (r["stage"], r["index"]) == ("IXSCAN", "ix_route")


def test_unknown_format_is_safe():
    assert parse_explain({"queryPlanner": {"Plan": ["x"]}})["stage"].startswith("n/a")
