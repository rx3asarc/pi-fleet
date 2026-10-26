"""Tests for fleet_restore -- the recovery path must not lie to be useful.

The dangerous failure here is not "cannot restore". It is "restored an answer that
nobody gave": an invented reaped_at, a guessed parent, a name made up to fill a
column. Every test below is one of two shapes:

  fidelity  what the inputs prove is exactly what comes back, field for field
  honesty   what no input proves comes back as null WITH a reason, never invented

RUN: cd /root/.pi/agent/bin && python3 -m unittest test_fleet_restore
"""

import json
import tempfile
import unittest
from pathlib import Path

import fleet_restore as R

SID = "01a1c0de-dead-7000-9000-000000000001"
PARENT = "01a1c0de-dead-7000-9000-0000000000ff"
CHILD2 = "01a1c0de-dead-7000-9000-000000000002"
PARENT_NAME = "engrane-main"
NAME = "engrane-citations"


def full_record(**kw):
    rec = {f: None for f in R.FIELDS}
    rec.update({
        "sessionId": SID, "name": NAME, "unit": "audit the citations appendix",
        "done_criteria": "", "transcript": None, "spawned_at": 1792246000,
        "reported_at": 1792246100, "report": "audited 41 citations",
        "keep_until": None, "reaped_at": 1792246200, "verdict": "reap",
        "why": "process gone, and it reported its unit",
        "digest": "/root/handoffs/_reaped/2026-10-17-x.md",
        "parent_session_id": PARENT, "parent_name": PARENT_NAME,
        "protected": None, "role": None,
    })
    rec.update(kw)
    return rec


class World:
    """Every input, in a temp dir. Nothing here can reach the real fleet."""

    def __init__(self, root):
        self.root = Path(root)
        self.names = self.root / "names"; self.names.mkdir()
        self.sessions = self.root / "sessions" / "d"; self.sessions.mkdir(parents=True)
        self.backups = self.root / "backups"; self.backups.mkdir()
        self.digests = self.root / "digests"; self.digests.mkdir()
        self.ledger = self.root / "ledger.jsonl"
        self.lineage = self.root / "lineage.json"
        self.ledger.write_text("")

    # -- writing inputs ----------------------------------------------------
    def name(self, sid, name, started_ms=None):
        (self.names / f"{sid[:20]}.json").write_text(json.dumps({
            "name": name, "sessionId": sid, "pid": 1, "tty": "pts/1",
            "startedAt": started_ms if started_ms is not None else 1792245900000,
            "updatedAt": 1792245900000}))

    def transcript(self, sid):
        p = self.sessions / f"2026-10-17T13-00-00-000Z_{sid}.jsonl"
        p.write_text('{"type":"message"}\n')
        return p

    def digest(self, sid, name, unit="audit the citations appendix",
               parent=f"{PARENT_NAME} ({PARENT[:8]})", reaped="2026-10-17T14:50:00Z",
               spawned="2026-10-17T13:46:40Z", verdict="reap — process gone"):
        p = self.digests / f"2026-10-17-{name}-{sid[:8]}.md"
        p.write_text(f"# {name} — reaped {reaped}\n\n- unit: {unit}\n"
                     f"- parent: {parent}\n- session: {sid}\n- spawned: {spawned}\n"
                     f"- verdict: {verdict}\n\n## What it said it did\n\nx\n")
        return p

    def row(self, **kw):
        row = {"ts": 1792246200, "action": "reap", "sessionId": SID, "name": NAME,
               "parent": PARENT, "why": "reported its unit",
               "digest": "/root/handoffs/_reaped/2026-10-17-x.md"}
        row.update(kw)
        with open(self.ledger, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def backup(self, children, stamp="20261017T150000Z"):
        p = self.backups / f"lineage-{stamp}.json"
        p.write_text(json.dumps({"version": 1, "children": children}, indent=2))
        return p

    def store(self, children):
        self.lineage.write_text(json.dumps({"version": 1, "children": children},
                                           indent=2))
        return children

    # -- the thing under test ---------------------------------------------
    def build(self):
        return R.build(self.names, str(self.sessions / "*.jsonl"), self.ledger,
                       self.backups, self.digests)

    def apply(self, proposal, **kw):
        return R.apply_proposal(proposal, self.lineage, self.backups, self.ledger, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.w = World(td.name)
        self.w.name(PARENT, PARENT_NAME)

    def rec(self, proposal, sid=SID):
        return proposal["children"][sid]


# ── fidelity ────────────────────────────────────────────────────────────────
class TestFidelity(Base):
    def test_proven_fields_match_an_existing_store(self):
        existing = {SID: full_record(), PARENT: full_record(
            **{"sessionId": PARENT, "name": PARENT_NAME, "parent_session_id": None})}
        self.w.store(existing)
        self.w.backup(existing)
        self.w.name(SID, NAME)
        self.w.row()
        self.w.digest(SID, NAME)
        proposal, _ = self.w.build()
        got = self.rec(proposal)
        for field, value in existing[SID].items():
            if value in (None, ""):
                continue          # an absent value is not a value to preserve
            self.assertEqual(got.get(field), value, f"{field} changed in restore")

    def test_the_name_registry_proves_a_name_and_an_enrolment_time(self):
        self.w.row()
        self.w.name(SID, NAME, started_ms=1792245900000)
        proposal, _ = self.w.build()
        rec = self.rec(proposal)
        self.assertEqual(rec["name"], NAME)
        self.assertEqual(rec["spawned_at"], 1792245900, "ms must be converted to s")
        self.assertEqual(rec["_provenance"]["name"], "names-registry")

    def test_the_ledger_proves_parentage_and_the_reap_event(self):
        self.w.row()
        proposal, _ = self.w.build()
        rec = self.rec(proposal)
        self.assertEqual(rec["parent_session_id"], PARENT)
        self.assertEqual(rec["reaped_at"], 1792246200)
        self.assertEqual(rec["verdict"], "reap")
        self.assertEqual(rec["_provenance"]["parent_session_id"], "ledger")

    def test_the_digest_proves_unit_spawn_and_reap_time(self):
        self.w.digest(SID, NAME)
        proposal, _ = self.w.build()
        rec = self.rec(proposal)
        self.assertEqual(rec["unit"], "audit the citations appendix")
        self.assertEqual(rec["spawned_at"], 1792244800)       # 13:46:40Z
        self.assertEqual(rec["reaped_at"], 1792248600)        # 14:50:00Z
        self.assertEqual(rec["_provenance"]["unit"], "digest")
        self.assertEqual(rec["parent_name"], PARENT_NAME,
                         "the digest records the parent's NAME, and that is proven")

    def test_a_transcript_proves_its_own_path(self):
        self.w.row()
        path = self.w.transcript(SID)
        proposal, _ = self.w.build()
        self.assertEqual(self.rec(proposal)["transcript"], str(path))
        self.assertEqual(self.rec(proposal)["_provenance"]["transcript"], "sessions")

    def test_backups_outrank_every_other_source(self):
        self.w.row()                       # ledger says reaped_at 1792246200
        self.w.backup({SID: full_record(reaped_at=1792249999, report="from backup")})
        proposal, _ = self.w.build()
        rec = self.rec(proposal)
        self.assertEqual(rec["reaped_at"], 1792249999)
        self.assertEqual(rec["report"], "from backup")
        self.assertTrue(rec["_provenance"]["reaped_at"].startswith("backup:"))


# ── honesty: never invent a value ───────────────────────────────────────────
class TestHonesty(Base):
    def test_unproven_fields_are_null_with_a_reason(self):
        self.w.row()
        proposal, _ = self.w.build()
        rec = self.rec(proposal)
        for field, reason in rec["_unproven"].items():
            self.assertIsNone(rec[field], f"{field} was invented as {rec[field]!r}")
            self.assertTrue(reason and len(reason) > 10, f"{field} has no reason")
        for field in ("reported_at", "report", "protected", "role", "keep_until",
                      "done_criteria"):
            self.assertIn(field, rec["_unproven"])

    def test_a_digest_parent_id_is_not_guessed_from_eight_characters(self):
        self.w.digest(SID, NAME)           # parent line has only the first 8 chars
        proposal, _ = self.w.build()
        rec = self.rec(proposal)
        self.assertIsNone(rec["parent_session_id"])
        self.assertIn("8 chars", rec["_unproven"]["parent_session_id"])
        self.assertEqual(rec["parent_name"], PARENT_NAME, "the name IS proven")

    def test_a_corrupt_store_does_not_become_a_confident_one(self):
        self.w.lineage.write_text("{ this is not json")
        self.w.row()
        proposal, _ = self.w.build()
        rec = self.rec(proposal)
        self.assertEqual(rec["name"], NAME)
        self.assertIsNone(rec["report"], "nothing may be invented because the store died")
        self.assertIn("report", rec["_unproven"])

    def test_no_record_is_created_from_a_name_registry_entry_alone(self):
        self.w.name(CHILD2, "never-a-fleet-session")
        proposal, _ = self.w.build()
        self.assertNotIn(CHILD2, proposal["children"])
        self.assertIn(CHILD2, proposal["_candidates"])
        self.assertEqual(proposal["children"], {}, "no fleet evidence, no record")

    def test_every_returned_record_is_a_valid_shape(self):
        self.w.row()
        self.w.name(SID, NAME)
        proposal, _ = self.w.build()
        self.assertEqual(len(proposal["children"]), 1,
                         "one row, one name, one digest => exactly one record; an "
                         "empty set would make the loop below assert nothing")
        for sid, rec in proposal["children"].items():
            self.assertEqual(rec["sessionId"], sid)
            self.assertIsInstance(rec["_provenance"], dict)
            self.assertIsInstance(rec["_unproven"], dict)


# ── the headline claim ──────────────────────────────────────────────────────
class TestLossReport(Base):
    def test_every_session_other_inputs_know_comes_back(self):
        self.w.name(SID, NAME)
        self.w.row()
        self.w.digest(SID, NAME)
        self.w.backup({SID: full_record(), PARENT: full_record(
            **{"sessionId": PARENT, "name": PARENT_NAME})})
        proposal, _ = self.w.build()
        cmp = R.compare(proposal, {})
        self.assertEqual(cmp["lost"], [], "the backup knows both records")
        self.assertEqual(cmp["records"], 2)

    def test_a_deleted_store_reports_nothing_lost(self):
        self.w.row()
        self.w.name(SID, NAME)          # name + enrolment time
        cmp = R.compare(*[self.w.build()[0], {}])
        self.assertEqual(cmp["lost"], [])
        self.assertEqual(cmp["complete"], 1, cmp["partial_fields"])

    def test_a_record_only_the_store_knew_is_reported_as_lost(self):
        self.w.row()
        proposal, _ = self.w.build()
        ghost = full_record(**{"sessionId": CHILD2, "name": "ghost"})
        cmp = R.compare(proposal, {CHILD2: ghost})
        self.assertEqual(len(cmp["lost"]), 1)
        self.assertIn("ghost", cmp["lost"][0])
        self.assertIn("1 would be LOST", R.headline(proposal, cmp))

    def test_the_headline_is_one_line(self):
        self.w.row()
        proposal, _ = self.w.build()
        line = R.headline(proposal, R.compare(proposal, {}))
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("come back", line)


# ── writing ─────────────────────────────────────────────────────────────────
class TestApply(Base):
    def seed_three(self):
        kids = {}
        for i in range(3):
            sid = f"01a1c0de-dead-7000-9000-00000000000{i}"
            kids[sid] = full_record(**{"sessionId": sid, "name": f"n{i}"})
        self.w.store(kids)
        self.w.backup(kids)
        return kids

    def test_it_never_writes_without_a_backup(self):
        kids = self.seed_three()
        proposal, _ = self.w.build()
        out = self.w.apply(proposal)
        self.assertTrue(out["written"])
        backup = Path(out["backup"])
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text())["children"], kids,
                         "the backup must be the store as it was")

    def test_it_refuses_to_shrink(self):
        self.seed_three()
        proposal, _ = self.w.build()
        proposal["children"] = {SID: full_record()}
        before = self.w.lineage.read_bytes()
        out = self.w.apply(proposal)
        self.assertFalse(out["written"])
        self.assertIn("refusing to shrink", out["reason"])
        self.assertEqual(self.w.lineage.read_bytes(), before, "a refusal writes nothing")

    def test_allow_shrink_is_an_explicit_override(self):
        self.seed_three()
        proposal, _ = self.w.build()
        proposal["children"] = {SID: full_record()}
        out = self.w.apply(proposal, allow_shrink=True)
        self.assertTrue(out["written"])
        self.assertEqual(len(json.loads(self.w.lineage.read_text())["children"]), 1)

    def test_it_records_itself_in_the_ledger(self):
        self.seed_three()
        proposal, _ = self.w.build()
        self.w.apply(proposal, now=1792250000)
        rows = [json.loads(l) for l in self.w.ledger.read_text().splitlines() if l.strip()]
        restores = [r for r in rows if r.get("action") == "restore"]
        self.assertEqual(len(restores), 1)
        self.assertEqual(restores[0]["scope"], "store")
        self.assertEqual(restores[0]["sessionId"], "*")
        self.assertEqual(restores[0]["records"], 3)
        self.assertEqual(restores[0]["ts"], 1792250000)

    def test_dry_run_is_the_default_and_touches_nothing(self):
        self.w.row()
        before = None
        if self.w.lineage.exists():
            before = self.w.lineage.read_bytes()
        proposal, _ = self.w.build()
        cmp = R.compare(proposal, {})
        self.assertEqual(self.w.lineage.read_bytes() if self.w.lineage.exists()
                         else None, before)
        self.assertEqual(cmp["records"], 1)

    def test_apply_on_a_missing_store_writes_a_fresh_one(self):
        self.w.row()
        proposal, _ = self.w.build()
        out = self.w.apply(proposal)
        self.assertTrue(out["written"])
        self.assertIsNone(out["backup"], "nothing to back up on a first write")
        self.assertTrue(self.w.lineage.exists())


class TestReadOnlyProof(Base):
    def test_build_changes_no_byte_of_any_input(self):
        self.w.row(); self.w.name(SID, NAME); self.w.digest(SID, NAME)
        self.w.backup({SID: full_record()})
        def snap():
            return {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                    for d in (self.w.names, self.w.backups, self.w.digests)
                    for p in sorted(d.iterdir())}
        before = snap()
        self.w.lineage.write_text("{}")
        lineage_before = self.w.lineage.read_bytes()
        self.w.build()
        self.assertEqual(snap(), before)
        self.assertEqual(self.w.lineage.read_bytes(), lineage_before)


class TestExitCodes(unittest.TestCase):
    """The exit code is the only thing a cron gate can read. "I refused to write"
    must never look like "I wrote" -- fork-3 had to mark this tool un-gateable
    for exactly that reason."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="restoreexit-"))
        self.lineage = self.tmp / "lineage.json"
        self.lineage.write_text(json.dumps({"version": 1, "children": {}}))

    def _main(self, apply_returns, *extra):
        from unittest import mock
        argv = ["--apply", "--lineage", str(self.lineage), *extra]
        with mock.patch.object(R, "apply_proposal", return_value=apply_returns):
            return R.main(argv)

    def test_a_refused_apply_exits_three_not_zero(self):
        rc = self._main({"written": False, "reason": "refusing to shrink the store"})
        self.assertEqual(rc, 3, "refusal was reported as success")

    def test_a_real_apply_still_exits_zero(self):
        self.assertEqual(self._main({"written": True, "reason": None}), 0)

    def test_dry_run_exits_zero_because_it_did_what_was_asked(self):
        from unittest import mock
        with mock.patch.object(R, "apply_proposal") as ap:
            rc = R.main(["--lineage", str(self.lineage)])
        self.assertEqual(rc, 0)
        ap.assert_not_called()




if __name__ == "__main__":
    unittest.main()
