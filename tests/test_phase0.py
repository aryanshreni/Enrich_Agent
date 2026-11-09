"""Offline unit tests for the Phase 0 contracts (no workspace calls). Run: pytest tests/"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.common import config, packet  # noqa: E402
from tools.readonly_sql import ReadOnlyViolation, assert_read_only  # noqa: E402


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "WITH a AS (SELECT 1 x) SELECT * FROM a",
    "DESCRIBE TABLE x.y.z",
    "SHOW TABLES IN x.y",
    "SELECT a FROM t UNION ALL SELECT b FROM u",
    "SELECT count(DISTINCT a) FROM `c`.`s`.`t` WHERE b IS NULL",
])
def test_guard_allows_reads(sql):
    assert_read_only(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE t", "INSERT INTO t SELECT 1", "SELECT 1; DROP TABLE t", "CREATE TABLE t AS SELECT 1",
    "UPDATE t SET a=1", "MERGE INTO t USING s ON t.a=s.a WHEN MATCHED THEN DELETE",
    "GRANT SELECT ON t TO x", "DELETE FROM t", "SHOW TABLES; DROP TABLE t", "OPTIMIZE t", "VACUUM t", "",
])
def test_guard_blocks_writes(sql):
    with pytest.raises(ReadOnlyViolation):
        assert_read_only(sql)


BASE = ("source_id: ns\nraw_location: {catalog: c, schema: raw}\n%s\ndescription: d\n"
        "reviewers: {data_engineer: a, data_owner: b, analyst: c}\n")


def test_config_valid():
    cfg = config.load_source_config(BASE % "target_location: {catalog: semantic_builder_poc, silver_schema: s, gold_schema: g}")
    assert cfg.raw_location.fqn == "c.raw"
    assert cfg.scope.selects("any_table")
    assert cfg.glossary.source == "none"


@pytest.mark.parametrize("target", [
    "target_location: {catalog: c, silver_schema: raw, gold_schema: g}",          # raw used as target
    "target_location: {catalog: semantic_builder_poc, silver_schema: 'x; drop', gold_schema: g}",  # bad ident
])
def test_config_rejects(target):
    with pytest.raises(Exception):
        config.load_source_config(BASE % target)


def _opt(i):
    return dict(option_id=i, label=f"Opt {i}", what_it_means="m", pros=["p"], cons=["c"], what_happens_next="n")


CARD = dict(
    card_key="dedup:t", risk_tier="high", title="How should duplicates be handled?",
    summary={"business": "s", "technical": "t"}, why_it_matters={"business": "w", "technical": "t"},
    evidence=[dict(narrative="412 records share an id", query_id="q1",
                   sql="SELECT id, count(*) FROM t GROUP BY id", columns=["id"], rows=[[1]])],
    options=[_opt("A"), _opt("B")],
    recommendation={"option_id": "A", "reason": "latest is a correction", "confidence": "High"},
)


def _packet(card):
    return dict(run_id="r", stage=2, stage_summary="s", artifact_path="/x", cards=[card], unlocks="u")


def test_packet_valid():
    assert packet.validate_packet(_packet(CARD)).cards[0].recommendation.option_id == "A"


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(options=[_opt("A")]),
    lambda c: c.update(options=[_opt(x) for x in "ABCD"] + [_opt("A")]),
    lambda c: c.update(options=[_opt("A"), _opt("A")]),
    lambda c: c["recommendation"].update(option_id="C"),
    lambda c: c["summary"].update(business="we ran SELECT id FROM t to check"),
    lambda c: c.update(title="Duplicates"),
    lambda c: c.update(risk_tier="low"),
    lambda c: c.update(evidence=[]),
], ids=["1-option", "5-options", "dup-ids", "rec-missing", "sql-in-business", "not-question", "low-tier-card", "no-evidence"])
def test_packet_rejects(mutate):
    c = copy.deepcopy(CARD)
    mutate(c)
    with pytest.raises(Exception):
        packet.validate_packet(_packet(c))


def test_artifact_paths():
    from tools.artifact_writer import ArtifactWriter

    w = ArtifactWriter(catalog="semantic_builder_poc", source_id="ns", run_id="r1", stage=2, attempt=1, client=object())
    assert w.path_for("pipeline/silver.sql") == "/Volumes/semantic_builder_poc/artifacts/files/ns/r1/s2_enrich/attempt_1/pipeline/silver.sql"
    for bad in ["../x", "/abs", "a b", "a/../b"]:
        with pytest.raises(ValueError):
            w.path_for(bad)
