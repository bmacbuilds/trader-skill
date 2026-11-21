"""The CLI's frozen surface: version, help, exit codes and the typed refusal (docs/cli-contract-v1.md)."""
from __future__ import annotations

import json

from traderos import __version__, cli


def test_version_and_help(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"traderos {__version__}"
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "never places a live order" in out and "Alpaca PAPER account" in out
    for command, _, _ in cli.COMMANDS:
        assert command in out
    assert "share" not in out                    # `passport share` is not built: help never offers it
    assert "explain | check-claims" in out


def test_passport_share_is_a_typed_refusal_not_a_usage_error(capsys):
    """Frozen in the v1 contract, not in this release: exit 3, CAPABILITY_DISABLED, in JSON too (never argparse's 2)."""
    assert cli.main(["--json", "passport", "share", "PP-x"]) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == "CAPABILITY_DISABLED" and failure["details"]["command"] == "passport share"
    assert cli.main(["passport", "share"]) == 3
    assert "not in this release" in capsys.readouterr().err


def test_doctor_reports_a_damaged_or_newer_store_as_a_fail_never_a_crash(tmp_path, capsys):
    import pathlib
    import sqlite3
    home = tmp_path / "junk"
    home.mkdir()
    (home / "registry.sqlite").write_bytes(b"this is not a database" * 100)
    assert cli.main(["--json", "--home", str(home), "doctor"]) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == "INTEGRITY_MISMATCH" and "is not a TraderOS store" in failure["details"]["store"]
    newer = str(tmp_path / "newer")
    assert cli.main(["--home", newer, "init"]) == 0
    with sqlite3.connect(pathlib.Path(newer) / "registry.sqlite") as db:
        db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    capsys.readouterr()
    assert cli.main(["--json", "--home", newer, "doctor"]) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == "SCHEMA_UNSUPPORTED" and "made by a newer TraderOS" in failure["details"]["store"]


def test_unknown_command_is_a_usage_error(capsys):
    assert cli.main(["buy", "SPY"]) == 2
    assert cli.main(["capabilities", "--bogus"]) == 2


def test_init_doctor_capabilities(tmp_path, capsys):
    home = str(tmp_path / "home")
    assert cli.main(["--json", "--home", home, "doctor"]) == 3               # a FAIL is never exit 0
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == "MISSING_DATA" and failure["details"]["store"].startswith("FAIL: no store")
    assert failure["details"]["python"].startswith("ok: ")                    # every check is still reported
    assert cli.main(["--home", home, "doctor"]) == 3
    err = capsys.readouterr().err
    assert "FAIL store" in err and "ok   python" in err and "traderos init" in err
    assert cli.main(["--home", home, "init"]) == 0
    assert cli.main(["--home", home, "init"]) == 0                            # idempotent
    capsys.readouterr()
    assert cli.main(["--json", "--home", home, "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["ok"] is True
    assert cli.main(["--json", "capabilities", "--all"]) == 0
    assert len(json.loads(capsys.readouterr().out)["result"]) == 22
    assert cli.main(["--json", "--home", home, "data", "list"]) == 0
    (tut,) = json.loads(capsys.readouterr().out)["result"]
    assert tut["synthetic"] is True
    assert cli.main(["--home", home, "data", "check", tut["dataset_id"]]) == 0
    assert "SYNTHETIC TUTORIAL DATA" in capsys.readouterr().out
    assert cli.main(["--json", "--home", home, "data", "check", "DS-nope"]) == 3


def test_unbuilt_command_refuses_typed(capsys, monkeypatch):
    # a frozen command no module registers yet; the real ones are all built by the top of the stack
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, ("unbuilt-example", "a frozen command", "nobody, test")))
    assert cli.main(["--json", "unbuilt-example"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["failure"]["code"] == "CAPABILITY_DISABLED"
    assert "nobody, test" in payload["failure"]["message"]


def test_demo_runs_offline_end_to_end_and_says_what_it_is(tmp_path, capsys, monkeypatch):
    from traderos import challenges
    monkeypatch.setattr(challenges, "CONTROLS", 2)
    home = str(tmp_path / "home")
    assert cli.main(["--home", home, "demo"]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "nothing places an order" in out
    assert "Verdict: insufficient evidence" in out and "reopened and verified" in out
    assert "2. Market Pulse as of" in out        # read from this --home, not the default folder
    assert cli.main(["--json", "--home", home, "memory", "search", "passport"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]
    # it runs again in the same store: the stored sample is reused and the same experiment replays as revision 2
    assert cli.main(["--json", "--home", home, "demo"]) == 0
    wf = json.loads(capsys.readouterr().out)["result"]
    assert wf["workflow_id"].endswith("-r2") and wf["completeness"] == "complete"
    assert "SYNTHETIC_DATA" in {lim["code"] for lim in wf["limitations"]}  # labelled in --json too
    assert cli.main(["--home", home, "demo"]) == 0
    out = capsys.readouterr().out
    assert "Already approved by an earlier demo run" in out and "revision 3" in out


def _demo_store(tmp_path, capsys, monkeypatch):
    from traderos import challenges
    monkeypatch.setattr(challenges, "CONTROLS", 2)
    home = str(tmp_path / "home")
    assert cli.main(["--home", home, "demo"]) == 0
    capsys.readouterr()
    return home


def test_a_rerun_refuses_a_tampered_demo_draft(tmp_path, capsys, monkeypatch):
    """OSDev4's review of #22: reusing the stored sample must still verify it. Bytes edited without re-hashing are
    INTEGRITY_MISMATCH, exit 3."""
    import pathlib
    import sqlite3
    home = _demo_store(tmp_path, capsys, monkeypatch)
    with sqlite3.connect(pathlib.Path(home) / "registry.sqlite") as db:
        n = db.execute("UPDATE records SET body = replace(body, 'tutorial-pullback', 'tutorial-pullbacX') "
                       "WHERE record_type = 'strategy-draft/1'").rowcount
    assert n == 1
    assert cli.main(["--json", "--home", home, "demo"]) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == "INTEGRITY_MISMATCH"
    assert "stored bytes do not match their hash" in failure["message"]       # refused on reading it back


def test_a_rerun_refuses_a_demo_draft_whose_rules_were_swapped_and_rehashed(tmp_path, capsys, monkeypatch):
    """A stored draft that verifies but no longer holds the sample's rules is refused: the demo would otherwise show
    the edited rules while running the stored version on the original ones."""
    import dataclasses
    import pathlib
    import sqlite3

    from traderos.commands.demo import DEMO_RULES
    from traderos.contracts import record_hash, to_json
    from traderos.strategies import rulebook
    home = _demo_store(tmp_path, capsys, monkeypatch)
    mine = rulebook.draft(DEMO_RULES)
    other = rulebook.draft({**DEMO_RULES, "exit": {"any": [{"bars_held": 3}]}})
    forged = dataclasses.replace(mine, rules=other.rules)                     # same id, other rules, re-hashed
    with sqlite3.connect(pathlib.Path(home) / "registry.sqlite") as db:
        n = db.execute("UPDATE records SET body = ?, canonical_hash = ? WHERE record_type = 'strategy-draft/1' "
                       "AND record_id = ?", (to_json(forged), record_hash(forged), mine.draft_id)).rowcount
    assert n == 1
    assert cli.main(["--json", "--home", home, "demo"]) == 3
    failure = json.loads(capsys.readouterr().out)["failure"]
    assert failure["code"] == "INTEGRITY_MISMATCH" and "no longer holds the demo's sample rules" in failure["message"]
