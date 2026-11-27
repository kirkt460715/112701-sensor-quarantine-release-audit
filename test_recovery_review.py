"""验证范围复核、证据时效、分批恢复和待办分类。"""
import json
from pathlib import Path
import tempfile
import unittest

from records_view import load_sources
from recovery_review import review


def write(directory, name, rows):
    (Path(directory) / f"{name}.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )


def load(assets, isolation, verification):
    with tempfile.TemporaryDirectory() as directory:
        write(directory, "assets", assets)
        write(directory, "isolation", isolation)
        write(directory, "verification", verification)
        return review(load_sources(Path(directory)))


def device(report, name):
    return next(item for item in report["devices"] if item["device"] == name)


class SampleRecordTests(unittest.TestCase):
    def setUp(self):
        self.report = review(load_sources(Path(__file__).with_name("records")))

    def test_replacement_is_checkable_and_transfers_only_that_unit(self):
        (replacement,) = self.report["replacements"]
        self.assertTrue(replacement["confirmed"])
        self.assertFalse(replacement["old_visible"])

        (scope,) = self.report["scope_reviews"]
        self.assertEqual(scope["effective_targets"], ["u-9", "u-6"])
        self.assertEqual(scope["retained"], ["u-6"])
        self.assertEqual(scope["allowed"], ["diagnostic"])
        self.assertEqual(scope["transferred"], [{"from": "u-5", "to": "u-9"}])

    def test_neighbor_is_not_released_by_shared_adapter(self):
        neighbor = device(self.report, "u-6")
        self.assertEqual(neighbor["disposition"], "isolated")
        self.assertIn("u-6", self.report["desk"]["still_isolated"])
        self.assertNotIn("u-6", self.report["desk"]["recovered"])

    def test_partial_scope_and_missing_execution_are_reported(self):
        self.assertEqual(self.report["desk"]["limited_recovery"], ["u-9"])
        self.assertEqual(self.report["desk"]["pending_execution"], ["u-9"])
        self.assertEqual(self.report["missing_evidence"]["execution_report_unknown"],
                         ["u-9"])
        self.assertEqual(self.report["missing_evidence"]["recheck_not_completed"],
                         ["u-6"])

    def test_old_unit_stays_retired_and_rechecks_need_evidence(self):
        old = device(self.report, "u-5")
        self.assertEqual(old["disposition"], "retired")
        self.assertFalse(old["recheckable"])
        self.assertIn("u-9", self.report["recheck_candidates"])
        self.assertIn("u-6", self.report["recheck_candidates"])

    def test_diagnostic_channel_remains_allowed(self):
        (scope,) = self.report["scope_reviews"]
        for unit in scope["effective_targets"]:
            self.assertIn("diagnostic", device(self.report, unit)["requirement"]["allowed"])


class EvidenceTests(unittest.TestCase):
    def test_late_pass_does_not_overwrite_newer_anomaly(self):
        report = load(
            [{"id": "u", "site": "p", "zone": "s", "id_seen": "u-1", "revision": "fw-b"}],
            [{"id": "iso", "targets": ["u-1"], "allowed": ["diagnostic"],
              "publish_requires": "fw-b"}],
            [
                {"id": "old-pass", "target": "u-1", "revision": "fw-b",
                 "result": "verified", "observed": 6, "arrived": 14},
                {"id": "new-fail", "target": "u-1", "revision": "fw-b",
                 "result": "failed", "observed": 13, "arrived": 9},
            ],
        )
        unit = device(report, "u-1")
        self.assertEqual(unit["observed"]["record"], "new-fail")
        self.assertEqual(unit["evidence"]["accepted"], [])
        reasons = {item["record"]: item["reason"] for item in unit["evidence"]["superseded"]}
        self.assertEqual(reasons["old-pass"], "older_observation")
        self.assertEqual(unit["disposition"], "isolated")
        self.assertIn("u-1", report["missing_evidence"]["recheck_not_completed"])

    def test_evidence_before_replacement_is_discarded(self):
        report = load(
            [
                {"id": "old", "site": "p", "zone": "s", "id_seen": "u-1",
                 "state": "removed"},
                {"id": "swap", "from": "u-1", "to": "u-2", "revision": "fw-b",
                 "site": "p", "day": 8},
                {"id": "new", "site": "p", "zone": "s", "id_seen": "u-2",
                 "revision": "fw-b"},
            ],
            [{"id": "iso", "targets": ["u-1"], "allowed": ["diagnostic"],
              "publish_requires": "fw-b"}],
            [
                {"id": "stale", "target": "u-1", "revision": "fw-b",
                 "result": "verified", "observed": 6},
                {"id": "fresh", "target": "u-2", "revision": "fw-b",
                 "result": "verified", "observed": 11},
            ],
        )
        self.assertTrue(report["replacements"][0]["confirmed"])
        self.assertEqual(device(report, "u-1")["disposition"], "retired")
        new = device(report, "u-2")
        self.assertEqual(new["evidence"]["discarded"], [])
        self.assertEqual(new["evidence"]["accepted"], ["fresh"])
        self.assertEqual(new["disposition"], "ready_for_approval")


class ReplacementTests(unittest.TestCase):
    def test_old_device_still_visible_blocks_release(self):
        report = load(
            [
                {"id": "old", "site": "p", "zone": "s", "id_seen": "u-1",
                 "state": "present"},
                {"id": "swap", "from": "u-1", "to": "u-2", "revision": "fw-b",
                 "site": "p", "day": 8},
            ],
            [{"id": "iso", "targets": ["u-1", "u-6"], "allowed": ["diagnostic"],
              "publish_requires": "fw-b"}],
            [
                {"id": "later", "target": "u-1", "revision": "fw-a",
                 "result": "verified", "observed": 9},
                {"id": "swap-check", "target": "u-2", "revision": "fw-b",
                 "result": "verified", "observed": 11},
            ],
        )
        (replacement,) = report["replacements"]
        self.assertFalse(replacement["confirmed"])
        self.assertEqual(replacement["old_visibility"], "observed_after_replacement")
        self.assertIn("old_device_still_visible", replacement["issues"])

        (scope,) = report["scope_reviews"]
        self.assertEqual(scope["effective_targets"], ["u-1", "u-6"])
        self.assertEqual(scope["transferred"], [])
        self.assertEqual(scope["retained"], ["u-1", "u-6"])

    def test_unidentified_new_device_is_separated_from_recheck_gap(self):
        report = load(
            [
                {"id": "old", "site": "p", "zone": "s", "id_seen": "u-1",
                 "state": "removed"},
                {"id": "swap", "from": "u-1", "to": "u-2", "revision": "fw-b",
                 "site": "p", "day": 8},
                {"id": "lookalike", "site": "q", "zone": "s", "id_seen": "u-2",
                 "revision": "fw-a"},
            ],
            [{"id": "iso", "targets": ["u-1"], "allowed": ["diagnostic"],
              "publish_requires": "fw-b"}],
            [{"id": "other-site", "target": "u-2", "revision": "fw-b",
              "result": "verified", "observed": 11}],
        )
        (replacement,) = report["replacements"]
        self.assertFalse(replacement["confirmed"])
        self.assertIn("new_device_revision_mismatch", replacement["issues"])
        self.assertIn("asset_not_identified", replacement["issues"])
        self.assertEqual(report["missing_evidence"]["asset_not_identified"], ["u-2"])


class PlanTests(unittest.TestCase):
    def test_partial_signoff_keeps_unresolved_scope(self):
        assets = [
            {"id": f"a-{n}", "site": "p", "zone": "s", "id_seen": f"u-{n}",
             "revision": "fw-b"}
            for n in (1, 2)
        ]
        verification = [
            {"id": f"check-{n}", "target": f"u-{n}", "revision": "fw-b",
             "result": "verified", "observed": 11}
            for n in (1, 2)
        ]
        report = load(
            assets,
            [
                {"id": "iso", "targets": ["u-1", "u-2"],
                 "allowed": ["diagnostic"], "publish_requires": "fw-b"},
                {"id": "restore", "targets": ["u-1", "u-2"], "intent": "upload",
                 "approved": True, "applied": ["u-1"]},
            ],
            verification,
        )
        (plan,) = report["plans"]
        self.assertEqual(plan["applied"], ["u-1"])
        self.assertEqual(plan["unresolved"], ["u-2"])
        self.assertEqual(plan["execution_report"], "partial")
        self.assertTrue(plan["partial"])
        self.assertEqual(report["desk"]["recovered"], ["u-1"])
        self.assertEqual(report["desk"]["pending_execution"], ["u-2"])

    def test_target_without_evidence_is_out_of_scope(self):
        report = load(
            [
                {"id": "a-1", "site": "p", "zone": "s", "id_seen": "u-1",
                 "revision": "fw-b"},
                {"id": "a-2", "site": "p", "zone": "s", "id_seen": "u-2",
                 "revision": "fw-b"},
            ],
            [
                {"id": "iso", "targets": ["u-1"], "allowed": ["diagnostic"],
                 "publish_requires": "fw-b"},
                {"id": "restore", "targets": ["u-1", "u-2"], "intent": "upload",
                 "approved": True, "applied": None},
            ],
            [{"id": "check-1", "target": "u-1", "revision": "fw-b",
              "result": "verified", "observed": 11}],
        )
        (plan,) = report["plans"]
        self.assertEqual(plan["eligible"], ["u-1"])
        self.assertEqual(plan["out_of_scope"], ["u-2"])
        self.assertIn("out_of_scope_targets", plan["issues"])
        self.assertEqual(report["desk"]["awaiting_evidence"], ["u-2"])
        self.assertNotIn("u-2", report["desk"]["recovered"])


if __name__ == "__main__":
    unittest.main()
