"""RED-FIRST PINS: what `dismiss` must mean, and what `reaped_at` must never mean.

This file is a SPEC written as failing tests. It is expected to be red until
fleet_children implements the contract; it is not a description of today's code.
Written after the auditor (fleet_audit.py) found the defect on the real store:

    i1_write_ahead  FAIL  5 records with reaped_at but closed by ['dismiss']
                          instead of reap -- the store and the ledger disagree
                          about what happened
    digests         FAIL  5 missing (the same five sids)

Why that is a modelling flaw and not a cosmetic one
--------------------------------------------------
`reaped_at` is the store's assertion *"a reap happened, and here is its receipt"*.
The digest and the write-ahead ledger both hang off that assertion. `dismiss` is a
different verb by design -- its own docstring says it is "deliberately separate
from reap: it never signals anything" -- but it sets `reaped_at` anyway. So a
dismissed record claims a reap it never had, and the digest check then demands a
receipt a dismiss never writes. Two stores disagree, and the only reason it never
bit us is that nothing had asked the question.

THE CONTRACT THIS FILE PINS (one sentence, for the implementation)

    `reaped_at` is set only by a reap and always has a digest; `dismiss` closes a
    record with `closed_at` / `closed_by="dismiss"` and no `reaped_at`; a record
    with either stamp is TERMINAL (verdict() HOLDs it, never REAPs or DECIDEs it,
    never revives it implicitly); and `revive` clears both stamps so the record has
    exactly one state again.

WHICH PINS ARE RED AT 2026-10-17T15:5xZ (14 tests: 7 red, 7 green)

  RED  1  dismiss_does_not_set_reaped_at          (sets reaped_at = now)
  RED  1  every_record_with_reaped_at_has_a_digest (5 real offenders on the store)
  RED  2  dismiss_sets_verdict_and_closed_fields   (no closed_at, no closed_by)
  RED  3  verdict_holds_a_closed_record            (verdict() has no closed_at case)
  RED  3  a_dismissed_record_never_comes_back_as_reap  -> returns reap
  RED  3  a_dismissed_record_is_not_implicitly_revived_by_the_cascade -> reap
  RED  4  revive_clears_both_stamps                (clears reaped_at, not closed_at)

  GREEN 2  dismiss_appends_one_ledger_row_naming_one_session
  GREEN 2  dismiss_kills_nothing_and_expects_no_digest
  GREEN 1  a_reap_still_sets_reaped_at_and_keeps_a_digest  (the contract must not
           be satisfied by simply never stamping -- this pin forbids that cheat)
  GREEN 4  revive_writes_a_ledger_row_for_the_revive
  GREEN 4  after_revive_the_record_is_evaluable_again  -- green *trivially* today,
           because closed_at does not exist yet; it starts meaning something the
           moment PIN 2 lands, and it is here so the fix cannot half-land
  GREEN 5  the_old_shape_is_refused_not_silently_accepted
  GREEN 5  the_auditor_does_not_migrate_it

The green pins are the walls around the fix: a change that satisfies the red ones
by removing the reap stamp, or by having the auditor rewrite the store, breaks a
green one.

RUN: cd /root/.pi/agent/bin && python3 -m unittest test_fleet_dismiss_semantics -v
(unittest only -- no pytest on this box)
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fleet_audit
import fleet_children
import fleet_lineage

NOW = 1792246200
SID = "01a1d15m-1550-7000-9000-000000000001"
PARENT_SID = "01a1d15m-1550-7000-9000-0000000000ff"


def child(**kw):
    rec = fleet_lineage.new_child(SID, "engrane-citations", PARENT_SID,
                                  "engrane-main", "audit the citations appendix", NOW)
    rec.update(kw)
    return rec


def facts(**kw):
    base = {"alive": True, "busy": False, "pending_ask": None,
            "parent_alive": True, "parent_finished": False, "last_seen": NOW - 30}
    base.update(kw)
    return base


class Store(unittest.TestCase):
    """Temp stores + inert deps. Nothing here can touch the real fleet."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.lineage_path = self.root / "lineage.json"
        self.ledger = self.root / "ledger.jsonl"
        self.digests = self.root / "digests"
        self.digests.mkdir()
        for name, path in (("LINEAGE", self.lineage_path), ("LEDGER", self.ledger),
                           ("DIGESTS", self.digests),
                           ("SENTINEL_REPORT", self.root / "sentinel.json")):
            p = mock.patch.object(fleet_children, name, path, create=True)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(fleet_children, "_D", {})
        p.start()
        self.addCleanup(p.stop)
        self.closed = []
        fleet_children.configure(
            entries=lambda: [],
            find=lambda name: None,
            sh=lambda argv, **kw: (1, "", "inert"),
            owner_tmux_of=lambda sid: "",
            die=lambda msg: (_ for _ in ()).throw(SystemExit(msg)),
            clip=lambda s, n: str(s or "")[:n],
            spawn_reviewer=lambda c, n: None,
            base="origin/main",
            safe_close=lambda sid, session, **kw: (
                self.closed.append(sid) or (True, "kill-pane inert:0.1")),
        )

    # -- store helpers -----------------------------------------------------
    def seed(self, rec):
        self.lineage_path.write_text(json.dumps(
            {"version": 1, "children": {rec["sessionId"]: rec}}, indent=2) + "\n")
        return rec

    def reload(self):
        return json.loads(self.lineage_path.read_text())["children"][SID]

    def rows(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text().splitlines() if l.strip()]

    def dismiss(self, note="decided by the parent"):
        self.seed(child())
        fleet_children.cmd_dismiss(SimpleNamespace(name="engrane-citations", note=note))
        return self.reload()

    def revive(self, rec):
        self.seed(rec)
        fleet_children.cmd_revive(SimpleNamespace(
            name="engrane-citations", note="brought back", unit=None))
        return self.reload()


# ── 1. reaped_at means reaped, with a receipt ───────────────────────────────
class TestReapedAtMeansReaped(Store):
    def test_dismiss_does_not_set_reaped_at(self):
        """PIN 1 (red): a dismiss must never claim that a reap happened."""
        rec = self.dismiss()
        self.assertIsNone(rec.get("reaped_at"),
                          "dismiss set reaped_at; the store now claims a reap "
                          "whose digest can never exist")

    def test_every_record_with_reaped_at_has_a_digest(self):
        """PIN 1 (red on the real store today): reap implies receipt."""
        path = Path("/root/.fleet/lineage.json")
        # NOT a skip: this check exists to inspect the real store, and this box
        # always has one. A skip here would report green while examining nothing —
        # the fourth such test found tonight (fixed by tundra, 2026-10-17).
        self.assertTrue(path.exists(), f"the real store is missing: {path}")
        kids = json.loads(path.read_text()).get("children") or {}
        liars = [f"{sid} ({r.get('name')}): reaped_at with no digest"
                 for sid, r in kids.items()
                 if isinstance(r, dict) and r.get("reaped_at") and not r.get("digest")]
        self.assertEqual(liars, [], "reaped_at is asserted without a receipt")

    def test_a_reap_still_sets_reaped_at_and_keeps_a_digest(self):
        """The contract must not be satisfied by never setting reaped_at."""
        rec = child(reported_at=NOW - 30, report="audited 41 citations")
        self.seed(rec)
        fleet_children._cascade(SimpleNamespace(
            apply=True, accept_silent=False, idle_after_min=30))
        rec = self.reload()
        self.assertIsNotNone(rec.get("reaped_at"), "a real reap must still stamp")
        self.assertTrue(rec.get("digest") and Path(rec["digest"]).exists(),
                        "a real reap must still write a digest")


# ── 2. what a dismiss is ────────────────────────────────────────────────────
class TestDismissContract(Store):
    def test_dismiss_sets_verdict_and_closed_fields(self):
        """PIN 2 (red): dismissed + closed_at + closed_by='dismiss'."""
        rec = self.dismiss()
        self.assertEqual(rec.get("verdict"), "dismissed")
        self.assertTrue(rec.get("closed_at"), "dismiss must record closed_at")
        self.assertEqual(rec.get("closed_by"), "dismiss")

    def test_dismiss_appends_one_ledger_row_naming_one_session(self):
        """PIN 2: the ledger row says what happened, to whom, exactly once."""
        self.dismiss()
        rows = [r for r in self.rows() if r.get("action") == "dismiss"]
        self.assertEqual(len(rows), 1, "one dismiss, one row")
        self.assertEqual(rows[0].get("sessionId"), SID)

    def test_dismiss_kills_nothing_and_expects_no_digest(self):
        """PIN 2: dismiss is bookkeeping. No kill, no receipt, no reap row."""
        rec = self.dismiss()
        self.assertEqual(self.closed, [], "dismiss must not signal anything")
        self.assertIsNone(rec.get("digest"), "dismiss does not produce a receipt")
        self.assertEqual([r for r in self.rows() if r.get("action") == "reap"], [],
                         "dismiss must not write a reap row")


# ── 3. terminal means terminal ──────────────────────────────────────────────
class TestTerminality(Store):
    def test_verdict_holds_a_closed_record(self):
        """PIN 3 (red): verdict() does not know closed_at, so it re-judges."""
        closed = child(verdict="dismissed", closed_at=NOW - 10, closed_by="dismiss")
        action, why = fleet_lineage.verdict(closed, facts(alive=True), NOW)
        self.assertEqual(action, "hold", f"a closed record was re-judged: {why}")
        self.assertIn("closed", why.lower())

    def test_a_dismissed_record_never_comes_back_as_reap(self):
        """PIN 3 (red): process gone must not turn a dismissal into a sweep."""
        closed = child(verdict="dismissed", closed_at=NOW - 10, closed_by="dismiss",
                       reported_at=NOW - 60, report="done")
        action, why = fleet_lineage.verdict(
            closed, facts(alive=False, parent_finished=True), NOW)
        self.assertNotEqual(action, "reap", f"a dismissed record was reaped: {why}")

    def test_a_dismissed_record_is_not_implicitly_revived_by_the_cascade(self):
        """PIN 3: a tick must not re-open a decision a human already made."""
        closed = self.seed(child(verdict="dismissed", closed_at=NOW - 10,
                                 closed_by="dismiss", reported_at=NOW - 60))
        fleet_children._cascade(SimpleNamespace(
            apply=True, accept_silent=False, idle_after_min=30))
        rec = self.reload()
        self.assertEqual(self.closed, [], "a closed record was killed")
        self.assertEqual(rec.get("verdict"), "dismissed", "the verdict was rewritten")
        self.assertEqual(rec.get("closed_at"), closed["closed_at"],
                         "the closing decision was silently undone")


# ── 4. revive restores exactly one state ────────────────────────────────────
class TestReviveContract(Store):
    def test_revive_clears_both_stamps(self):
        """PIN 4 (red): one state, not 'reaped but closed but active'."""
        old = child(verdict="dismissed", closed_at=NOW - 10, closed_by="dismiss",
                    reaped_at=NOW - 20, digest="/tmp/x.md")
        rec = self.revive(old)
        self.assertIsNone(rec.get("reaped_at"), "revive must clear reaped_at")
        self.assertIsNone(rec.get("closed_at"), "revive must clear closed_at")
        self.assertIsNone(rec.get("closed_by"), "revive must clear closed_by")
        self.assertNotEqual(rec.get("verdict"), "dismissed",
                            "revive must leave the record evaluable")

    def test_after_revive_the_record_is_evaluable_again(self):
        """PIN 4: exactly one verdict, and it is a live one (not a terminal HOLD)."""
        old = child(verdict="dismissed", closed_at=NOW - 10, closed_by="dismiss",
                    reaped_at=NOW - 20, digest="/tmp/x.md")
        rec = self.revive(old)
        action, why = fleet_lineage.verdict(
            rec, facts(alive=True, last_seen=NOW - 60), NOW)
        self.assertIn(action, ("hold", "notify", "reap", "decide"),
                      f"unknown action {action!r}")
        self.assertNotIn("closed", why.lower(),
                         f"a revived record is still treated as closed: {why}")

    def test_revive_writes_a_ledger_row_for_the_revive(self):
        """PIN 4: coming back is an event, and events are in the ledger."""
        old = child(verdict="dismissed", closed_at=NOW - 10, closed_by="dismiss")
        self.revive(old)
        self.assertEqual([r.get("action") for r in self.rows()], ["revive"])


# ── 5. migration: declared, and pinned ──────────────────────────────────────
class TestMigrationIsRefusedLoudly(Store):
    """DECLARATION: the auditor does NOT migrate. It refuses, loudly.

    A read-only auditor that rewrites a store is a repairer, and a repairer that
    can be wrong is worse than a checker that says "I don't know". The five real
    records written by the old `dismiss` are exactly the shape below; after the
    fix they are migrated by whoever owns the store (fork-1), not by this tool.
    """

    OLD_SIDS = [
        "01a14a29-7461-7455-867c-f856830a4e80",
        "01a14a33-261c-74f8-ac82-e29eff759caf",
        "01a14a35-a12b-7616-94ab-58764ab7d71a",
        "01a14a7c-fc3e-7450-8d39-a889d7a566c0",
        "01a14a7c-bc69-74e5-9175-5350d27e83e7",
    ]

    def build_old_shape(self):
        kids, rows = {}, []
        for i, sid in enumerate(self.OLD_SIDS):
            rec = fleet_lineage.new_child(sid, f"old-dismiss-{i}", None, None,
                                          "closed by the old dismiss", NOW - 100)
            rec.update(reported_at=NOW - 90, report="done", verdict="dismissed",
                       reaped_at=NOW - 80, protected=True, digest=None)
            kids[sid] = rec
            rows.append({"ts": NOW - 80, "action": "dismiss", "sessionId": sid,
                         "name": rec["name"], "why": "dismissed by the parent"})
        self.lineage_path.write_text(json.dumps(
            {"version": 1, "children": kids}, indent=2) + "\n")
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return kids

    def test_the_old_shape_is_refused_not_silently_accepted(self):
        self.build_old_shape()
        report = fleet_audit.run(self.lineage_path, self.ledger)
        chk = next(c for c in report["checks"] if c["check"] == "i1_write_ahead")
        self.assertEqual(chk["status"], "FAIL")
        self.assertIn("dismiss", chk["detail"])
        self.assertEqual(report["exit_code"], 1)
        named = "\n".join(chk["ids"])
        for sid in self.OLD_SIDS:
            self.assertIn(sid, named, f"{sid} was not named")

    def test_the_auditor_does_not_migrate_it(self):
        kids = self.build_old_shape()
        before = self.lineage_path.read_bytes()
        fleet_audit.run(self.lineage_path, self.ledger)
        self.assertEqual(self.lineage_path.read_bytes(), before,
                         "a read-only auditor rewrote the store")
        after = json.loads(self.lineage_path.read_text())["children"]
        for sid in self.OLD_SIDS:
            self.assertEqual(after[sid].get("reaped_at"), kids[sid]["reaped_at"])
            self.assertIsNone(after[sid].get("closed_at"),
                              "the auditor migrated; it must refuse instead")


if __name__ == "__main__":
    unittest.main()
