"""A lab command run again in the same store (docs/cli-contract-v1.md).

`strategy draft` of the same file, or `strategy approve` of the same draft, run twice: the second run replays the
stored record (exit 0) instead of refusing INTEGRITY_MISMATCH, and a tampered record is still refused.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from traderos import cli
from traderos.commands.demo import DEMO_RULES
from traderos.contracts import StrategyDraft, StrategyVersion, from_json, record_hash, to_json, with_hash
from traderos.store import Store


def run(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(["--json", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


@pytest.fixture
def drafted(tmp_path, capsys):
    """A home with the sample rules drafted and approved once; returns (home, rules file, draft, version)."""
    home, path = str(tmp_path / "home"), tmp_path / "rules.json"
    path.write_text(json.dumps(DEMO_RULES), encoding="utf-8")
    code, out = run(capsys, "--home", home, "strategy", "draft", str(path))
    assert code == 0, out
    d = out["result"]
    code, out = run(capsys, "--home", home, "strategy", "approve", d["draft_id"], "--confirm", d["semantic_hash"][:12])
    assert code == 0, out
    return home, str(path), from_json(StrategyDraft, json.dumps(d)), from_json(StrategyVersion,
                                                                              json.dumps(out["result"]))


def _rewrite(home: str, schema: str, body: str, digest: str | None = None) -> None:
    sql = "UPDATE records SET body = ?" + (", canonical_hash = ?" if digest else "") + " WHERE record_type = ?"
    with Store(home).connect() as db:
        db.execute(sql, (body, digest, schema) if digest else (body, schema))


# ---- strategy draft / approve, run again -------------------------------------------------------------------------

def test_strategy_draft_and_approve_twice_replay_the_stored_record(drafted, capsys):
    home, path, d, v = drafted
    code, out = run(capsys, "--home", home, "strategy", "draft", path)
    assert code == 0 and out["result"] == json.loads(to_json(d))           # the stored draft, its first time included
    approve = ("--home", home, "strategy", "approve", d.draft_id, "--confirm", d.semantic_hash[:12])
    code, out = run(capsys, *approve)
    assert code == 0 and out["result"] == json.loads(to_json(v))           # the first approval stands
    assert cli.main(["--home", home, "strategy", "draft", path]) == 0
    assert f"Draft {d.draft_id} was already saved" in capsys.readouterr().out
    assert cli.main(list(approve)) == 0
    assert f"Already approved: {v.version_id}" in capsys.readouterr().out
    store = Store(home)
    assert store.all(StrategyDraft) == [d] and store.all(StrategyVersion) == [v]


def test_a_rerun_still_refuses_a_tampered_draft(drafted, capsys):
    home, path, d, _ = drafted
    approve = ("--home", home, "strategy", "approve", d.draft_id, "--confirm", d.semantic_hash[:12])
    original = to_json(d)
    _rewrite(home, StrategyDraft.SCHEMA, original.replace("tends to recover", "tends to RECOVER"))   # hash left
    for argv in (("--home", home, "strategy", "draft", path), approve):
        code, out = run(capsys, *argv)
        assert code == 3 and out["failure"]["code"] == "INTEGRITY_MISMATCH", argv
    forged = dataclasses.replace(d, rules={**d.rules, "entry": {"below": [{"rsi": 2}, 15]}})
    _rewrite(home, StrategyDraft.SCHEMA, to_json(forged), record_hash(forged))    # rules edited and re-hashed
    for argv in (("--home", home, "strategy", "draft", path), approve):
        code, out = run(capsys, *argv)
        assert code == 3 and out["failure"]["code"] == "INTEGRITY_MISMATCH", argv
        assert "do not match" in out["failure"]["message"]
    _rewrite(home, StrategyDraft.SCHEMA, original, record_hash(d))
    code, out = run(capsys, "--home", home, "strategy", "draft", path, "--author", "ai")   # same id, other author
    assert code == 3 and "different author_type" in out["failure"]["message"]


def test_a_rerun_still_refuses_a_tampered_version(drafted, capsys):
    home, _, d, v = drafted
    approve = ("--home", home, "strategy", "approve", d.draft_id, "--confirm", d.semantic_hash[:12])
    _rewrite(home, StrategyVersion.SCHEMA, to_json(v).replace("tends to recover", "tends to RECOVER"))
    code, out = run(capsys, *approve)
    assert code == 3 and out["failure"]["code"] == "INTEGRITY_MISMATCH"
    forged = with_hash(dataclasses.replace(v, rules={**v.rules, "entry": {"below": [{"rsi": 2}, 15]}}))
    _rewrite(home, StrategyVersion.SCHEMA, to_json(forged), record_hash(forged))
    code, out = run(capsys, *approve)
    assert code == 3 and out["failure"]["code"] == "INTEGRITY_MISMATCH" and "do not match" in out["failure"]["message"]
