"""U7 — identity repair and a single writer.

PURE. Fixtures only. No filesystem, no tmux, no git.

WHAT REPAIR ACTUALLY IS (measured 2026-10-11, not assumed)
The registry already stores the pi name, not the drifted tmux name:
`session-names/01a116b1-...json` holds `name: "plover-wren"` while tmux still calls
that session `globex-rowan`. So the 5 known-drifted sessions need NO registry
repair at all -- the drift lives between tmux and pi, and U3 already REPORTS it.

What is genuinely repairable is a record whose stored name is empty or wrong:
1 of 53 records reads `name: ""` while pi calls that session `willow`.

So repair is narrow on purpose: make the identity store agree with the canonical
(the pi) name, touch nothing else, and never rewrite history.

FORWARD-ONLY
Renaming a live tmux session is NOT repair. It manufactures the drift bug rather
than fixing it, and the objective forbids it outside a deliberate pass. Repair
therefore writes the store forward and leaves every historical field alone.
"""
import json
import unittest

from pi_identity import canonical_name, plan_repair, offenders, WRITER

# (tmux name, pid, pi name, sessionId) — the 5 live drift cases from U2.
DRIFTED = [
    ("jasper-alder", 2865745, "amber", "01a11ade-2f99-7230-9c83-e649c4009090"),
    ("globex-rowan", 431941, "plover-wren", "01a116b1-3f3a-76dd-b584-7accd993086f"),
    ("cobalt-rowan", 2439092, "cobalt", "01a11a38-1111-2222-3333-444455556666"),
    ("larch", 2522560, "acme-goal-helper-6", "01a11a63-1111-2222-3333-444455556666"),
    ("nimbus", 2573667, "acme-goal-helper-7", "01a11a75-1111-2222-3333-444455556666"),
]


def drifted_rows():
    return [{"sessionId": sid, "name": pi_name, "tmux": tmux, "pid": pid}
            for tmux, pid, pi_name, sid in DRIFTED]


def record(sid, name, **extra):
    r = {"sessionId": sid, "name": name, "pid": 1, "topic": "keep me",
         "startedAt": 111, "updatedAt": 222}
    r.update(extra)
    return r


class TestOneCanonicalIdentityPerSession(unittest.TestCase):
    def test_the_five_drift_cases_each_resolve_to_one_identity(self):
        plan = plan_repair(drifted_rows(), [])
        self.assertEqual(len(plan), 5)
        for item in plan:
            with self.subTest(sid=item["sessionId"]):
                self.assertEqual(item["canonical"], dict(
                    zip([d[3] for d in DRIFTED], [d[2] for d in DRIFTED]))[item["sessionId"]])

    def test_the_canonical_name_is_the_pi_name_not_the_tmux_name(self):
        """The tmux name is an attribute of the row, not its identity."""
        for tmux, _pid, pi_name, sid in DRIFTED:
            with self.subTest(tmux=tmux):
                self.assertEqual(canonical_name({"sessionId": sid, "name": pi_name,
                                                 "tmux": tmux}), pi_name)

    def test_an_already_canonical_record_is_left_alone(self):
        rows = drifted_rows()
        regs = [record(r["sessionId"], r["name"]) for r in rows]
        plan = plan_repair(rows, regs)
        self.assertEqual([i["action"] for i in plan], ["none"] * 5)

    def test_an_empty_stored_name_is_repaired(self):
        """The one real defect found: 1 of 53 records has name: ''."""
        rows = [{"sessionId": "sid-w", "name": "willow", "tmux": "willow", "pid": 9}]
        plan = plan_repair(rows, [record("sid-w", "")])
        self.assertEqual(plan[0]["action"], "rewrite")
        self.assertEqual(plan[0]["canonical"], "willow")

    def test_a_wrong_stored_name_is_repaired(self):
        rows = [{"sessionId": "sid-x", "name": "amber", "tmux": "jasper-alder", "pid": 3}]
        plan = plan_repair(rows, [record("sid-x", "jasper-alder")])
        self.assertEqual(plan[0]["action"], "rewrite")
        self.assertEqual(plan[0]["canonical"], "amber")

    def test_a_record_with_no_live_session_is_not_touched(self):
        """Dead sessions keep whatever they were; repair is not garbage collection."""
        plan = plan_repair([], [record("sid-gone", "old-name")])
        self.assertEqual(plan, [])


class TestForwardOnly(unittest.TestCase):
    def test_repair_never_rewrites_history(self):
        """Only `name` may move. Every historical field must be carried verbatim."""
        rows = [{"sessionId": "sid-x", "name": "amber", "tmux": "jasper-alder", "pid": 3}]
        reg = record("sid-x", "wrong", topic="original topic", startedAt=5, updatedAt=7)
        item = plan_repair(rows, [reg])[0]
        self.assertEqual(item["patch"], {"name": "amber"})
        for field in ("topic", "startedAt", "updatedAt", "pid"):
            self.assertNotIn(field, item["patch"])

    def test_repair_never_proposes_a_delete_or_a_rename_of_tmux(self):
        rows = drifted_rows()
        regs = [record(r["sessionId"], "") for r in rows]
        plan = plan_repair(rows, regs)
        self.assertFalse([i for i in plan if i["action"] not in ("none", "rewrite")])
        self.assertFalse([i for i in plan if "tmux" in i["patch"]])


class TestSingleWriter(unittest.TestCase):
    def test_the_plan_names_exactly_one_writer(self):
        self.assertTrue(WRITER)

    def test_a_module_writing_the_registry_directly_is_reported(self):
        """A rename attempted by any other path must be refused or reported."""
        bad = {"rogue.py": "import json\n"
                           "open('/root/.pi/agent/session-names/x.json','w').write('{}')\n"}
        self.assertEqual(offenders(bad), ["rogue.py"])

    def test_a_compliant_module_is_not_reported(self):
        good = {"fine.py": "from pi_identity import apply_repair\n"
                           "apply_repair(plan)\n"}
        self.assertEqual(offenders(good), [])

    def test_the_offender_scan_ignores_prose(self):
        """A docstring mentioning the path is not a write."""
        ok = {"docs.py": '"""We never write session-names/*.json directly."""\n'}
        self.assertEqual(offenders(ok), [])


class TestDriftIsAlreadyAHealthMetric(unittest.TestCase):
    def test_drift_is_reported_alongside_unscoped(self):
        """U7's fourth RED item landed with U3; asserted here so it cannot regress."""
        from pi_view import is_drifted, summary
        self.assertTrue(is_drifted({"name": "amber", "tmux": "jasper-alder"}))
        self.assertFalse(is_drifted({"name": "amber", "tmux": "amber"}))
        s = summary([{"name": "amber", "tmux": "jasper-alder", "state": "idle",
                      "domain": "unscoped", "topic": "", "subtopic": ""}])
        self.assertIn("drift", s)
        self.assertIn("unscoped", s)


if __name__ == "__main__":
    unittest.main()


class TestOffendersSeesWritesNotReads(unittest.TestCase):
    """The auditor's objection, 2026-10-20: `--check-writers` exited 1 naming
    `fleet_children.py` and `fleet_restore.py`, both of which only READ the store.
    The detector was FILE-scoped on "does this file write anything at all", so a
    module that reads the store and writes some unrelated report was convicted.

    A guard that reports false offenders is worse than no guard: it teaches the
    reader to ignore the exit code, and the one real offence then walks through.
    """

    READER = '''
import json
from pathlib import Path
NAMES_DIR = Path("/x/session-names")
def load_names():
    return {p.stem: json.loads(p.read_text()) for p in NAMES_DIR.glob("*.json")}
def write_report(path, text):
    path.write_text(text)
'''

    WRITER_VIA_ALIAS = '''
import json
from pathlib import Path
NAMES_DIR = Path("/x/session-names")
def save(rec):
    tmp = NAMES_DIR / (rec["id"] + ".json")
    tmp.write_text(json.dumps(rec))
'''

    WRITER_BY_LITERAL = '''
import json
def save(rec, base):
    json.dump(rec, open(base + "/session-names/" + rec["id"] + ".json", "w"))
'''

    def test_a_module_that_reads_the_store_and_writes_elsewhere_is_not_convicted(self):
        self.assertEqual(offenders({"fleet_children.py": self.READER}, allow={}), [])

    def test_a_module_that_writes_the_store_through_an_alias_is_still_convicted(self):
        self.assertEqual(offenders({"w.py": self.WRITER_VIA_ALIAS}, allow={}), ["w.py"])

    def test_a_module_that_writes_the_store_by_literal_is_still_convicted(self):
        self.assertEqual(offenders({"l.py": self.WRITER_BY_LITERAL}, allow={}), ["l.py"])

    def test_declared_writers_are_skipped(self):
        self.assertEqual(offenders({"w.py": self.WRITER_VIA_ALIAS}, allow={"w.py"}), [])

    def test_a_docstring_mentioning_the_store_is_not_an_offence(self):
        prose = '"""We must never write session-names from here."""\nX = 1\n'
        self.assertEqual(offenders({"p.py": prose}, allow={}), [])
