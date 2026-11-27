"""合成登记规则的单元测试：隔离、范围复核、分批恢复。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from records_view import load_sources
from review import (
    GAP_ASSET,
    GAP_EXECUTION,
    GAP_RECHECK,
    LIMITED_RECOVERY,
    PENDING_EXECUTION,
    build_registry,
    console_report,
    evidence_backed,
    policy_vs_observed,
)

ROOT = Path(__file__).resolve().parent


def write_sources(directory, assets, isolation, verification):
    for name, rows in (("assets", assets), ("isolation", isolation),
                       ("verification", verification)):
        (Path(directory) / f"{name}.json").write_text(
            json.dumps(rows), encoding="utf-8")


def registry_from(assets, isolation, verification):
    with tempfile.TemporaryDirectory() as directory:
        write_sources(directory, assets, isolation, verification)
        return build_registry(load_sources(Path(directory)))


def by_target(entries):
    return {entry["target"]: entry for entry in entries}


BASE_ASSETS = [
    {"id": "unit-old", "site": "p1", "zone": "sensors",
     "id_seen": "u-5", "state": "removed"},
    {"id": "replacement", "from": "u-5", "to": "u-9",
     "revision": "fw-b", "site": "p1", "day": 8},
    {"id": "unit-neighbor", "site": "p1", "zone": "sensors",
     "id_seen": "u-6", "revision": "fw-a"},
]
BASE_ISOLATION = [
    {"id": "iso-5", "targets": ["u-5", "u-6"],
     "allowed": ["diagnostic"], "publish_requires": "fw-b"},
    {"id": "restore-n", "targets": ["u-9"], "intent": "upload",
     "approved": True, "applied": None},
]
BASE_VERIFICATION = [
    {"id": "check-n", "target": "u-9", "revision": "fw-b",
     "result": "verified", "observed": 11},
    {"id": "late-pass", "target": "u-5", "revision": "fw-a",
     "result": "verified", "observed": 6, "arrived": 14},
    {"id": "look-6", "target": "u-6", "revision": "fw-a",
     "result": "pending"},
]


class CanonicalRecordTests(unittest.TestCase):
    def setUp(self):
        self.entries = by_target(
            build_registry(load_sources(ROOT / "records")))

    def test_qualified_replacement_is_pending_not_auto_recovered(self):
        u9 = self.entries["u-9"]
        self.assertTrue(u9["evidence_qualifies"])
        self.assertEqual(u9["status"], PENDING_EXECUTION)
        self.assertEqual(u9["upload"], "approved_pending")
        self.assertEqual(u9["evidence_gap"], GAP_EXECUTION)
        self.assertTrue(u9["replacement"]["attributable"])
        self.assertEqual(u9["scope"]["via_replacement"], "u-5")
        # 未签收：系统不代现场执行
        self.assertIsNone(u9["plan"]["applied"])

    def test_neighbor_not_unblocked_by_shared_forwarder(self):
        u6 = self.entries["u-6"]
        self.assertFalse(u6["evidence_qualifies"])
        self.assertEqual(u6["status"], LIMITED_RECOVERY)
        self.assertEqual(u6["upload"], "blocked")
        self.assertEqual(u6["evidence_gap"], GAP_RECHECK)

    def test_legitimate_diagnostic_path_is_preserved(self):
        for target in ("u-5", "u-6", "u-9"):
            with self.subTest(target=target):
                self.assertIn("diagnostic", self.entries[target]["retained_uses"])

    def test_late_old_pass_does_not_override_current_gap(self):
        u5 = self.entries["u-5"]
        self.assertIn("late-pass", u5["evidence"]["stale_late_passes"])
        self.assertFalse(u5["evidence_qualifies"])
        self.assertEqual(u5["policy"]["required_revision"], "fw-b")
        self.assertEqual(u5["evidence"]["revision"], "fw-a")
        self.assertEqual(u5["evidence_gap"], GAP_RECHECK)
        self.assertEqual(u5["observed"]["succeeded_by"], "u-9")

    def test_replacement_proof_checks_old_device_visibility(self):
        u9 = self.entries["u-9"]
        self.assertFalse(u9["replacement"]["old_still_visible"])
        self.assertTrue(u9["replacement"]["attributable"])

    def test_console_buckets_partition_all_targets(self):
        report = console_report(load_sources(ROOT / "records"))
        buckets = report["buckets"]
        self.assertEqual(buckets["limited_recovery"], ["u-5", "u-6"])
        self.assertEqual(buckets["pending_execution"], ["u-9"])
        self.assertEqual(buckets["still_isolated"], [])
        self.assertEqual(buckets["recovered"], [])
        listed = sum(buckets.values(), [])
        self.assertEqual(sorted(listed), ["u-5", "u-6", "u-9"])

    def test_policy_vs_observed_flags_version_gap(self):
        rows = {row["target"]: row
                for row in policy_vs_observed(list(self.entries.values()))}
        self.assertTrue(rows["u-9"]["revision_match"])
        self.assertFalse(rows["u-5"]["revision_match"])
        self.assertFalse(rows["u-6"]["revision_match"])

    def test_only_evidence_backed_devices_can_be_re_reviewed(self):
        backed = {entry["target"] for entry
                  in evidence_backed(load_sources(ROOT / "records"))}
        self.assertEqual(backed, {"u-9"})
        filtered = console_report(load_sources(ROOT / "records"),
                                  only_evidenced=True)
        self.assertEqual(
            sum(filtered["buckets"].values(), []), ["u-9"])


class EdgeRuleTests(unittest.TestCase):
    def test_old_device_still_visible_blocks_attribution(self):
        assets = [dict(row) for row in BASE_ASSETS]
        assets[0]["state"] = "active"  # 旧设备仍在观察点可见
        entries = by_target(registry_from(assets, BASE_ISOLATION,
                                          BASE_VERIFICATION))
        u9 = entries["u-9"]
        self.assertTrue(u9["replacement"]["old_still_visible"])
        self.assertFalse(u9["replacement"]["attributable"])
        self.assertFalse(u9["evidence_qualifies"])
        self.assertEqual(u9["evidence_gap"], GAP_ASSET)
        self.assertIn("old_device_still_visible", u9["reasons"])
        # 已核准的恢复计划不能落在归属不明的设备上
        self.assertIn("plan_target_out_of_scope", u9["reasons"])
        self.assertNotEqual(u9["status"], PENDING_EXECUTION)

    def test_unseen_plan_target_is_asset_unrecognized(self):
        isolation = BASE_ISOLATION + [
            {"id": "restore-stray", "targets": ["u-77"],
             "intent": "upload", "approved": True, "applied": None}]
        verification = BASE_VERIFICATION + [
            {"id": "c77", "target": "u-77", "revision": "fw-b",
             "result": "verified", "observed": 10}]
        entries = by_target(registry_from(BASE_ASSETS, isolation,
                                          verification))
        u77 = entries["u-77"]
        self.assertIsNone(u77["replacement"])
        self.assertIsNone(u77["scope"]["decision_id"])
        self.assertFalse(u77["plan_in_scope"])
        self.assertFalse(u77["evidence_qualifies"])
        self.assertEqual(u77["evidence_gap"], GAP_ASSET)
        self.assertIn("plan_target_out_of_scope", u77["reasons"])

    def test_unattributable_new_device_plan_is_out_of_scope(self):
        isolation = BASE_ISOLATION + [
            {"id": "restore-x", "targets": ["u-100"], "intent": "upload",
             "approved": True, "applied": None}]
        verification = BASE_VERIFICATION + [
            {"id": "c100", "target": "u-100", "revision": "fw-b",
             "result": "verified", "observed": 10}]
        entries = by_target(registry_from(BASE_ASSETS, isolation,
                                          verification))
        u100 = entries["u-100"]
        self.assertFalse(u100["plan_in_scope"])
        self.assertFalse(u100["evidence_qualifies"])
        self.assertEqual(u100["evidence_gap"], GAP_ASSET)
        self.assertNotEqual(u100["status"], PENDING_EXECUTION)

    def test_partial_sign_off_leaves_remaining_scope_pending(self):
        isolation = [
            {"id": "iso-5", "targets": ["u-5", "u-6"],
             "allowed": ["diagnostic"], "publish_requires": "fw-b"},
            {"id": "restore-p", "targets": ["u-5", "u-6"],
             "intent": "upload", "approved": True,
             "applied": ["u-5"]},
        ]
        verification = [
            {"id": "c5", "target": "u-5", "revision": "fw-b",
             "result": "verified", "observed": 12},
            {"id": "c6", "target": "u-6", "revision": "fw-b",
             "result": "verified", "observed": 12},
        ]
        entries = by_target(registry_from(BASE_ASSETS, isolation,
                                          verification))
        self.assertEqual(entries["u-5"]["status"], "recovered")
        self.assertEqual(entries["u-5"]["upload"], "active")
        self.assertEqual(entries["u-6"]["status"], PENDING_EXECUTION)
        self.assertEqual(entries["u-6"]["upload"], "approved_pending")
        self.assertEqual(entries["u-6"]["evidence_gap"], GAP_EXECUTION)

    def test_new_anomaly_not_overridden_by_late_old_pass(self):
        verification = [
            {"id": "anomaly", "target": "u-5", "revision": "fw-b",
             "result": "failed", "observed": 13},
            {"id": "late-pass", "target": "u-5", "revision": "fw-a",
             "result": "verified", "observed": 6, "arrived": 14},
        ]
        entries = by_target(registry_from(BASE_ASSETS, BASE_ISOLATION,
                                          verification))
        u5 = entries["u-5"]
        self.assertEqual(u5["evidence"]["latest"], "anomaly")
        self.assertIn("late-pass", u5["evidence"]["stale_late_passes"])
        self.assertFalse(u5["evidence_qualifies"])
        self.assertEqual(u5["evidence_gap"], GAP_RECHECK)

    def test_no_allowed_uses_means_still_isolated(self):
        isolation = [
            {"id": "iso-hard", "targets": ["u-8"],
             "allowed": [], "publish_requires": "fw-b"}]
        entries = by_target(registry_from(BASE_ASSETS, isolation,
                                          BASE_VERIFICATION))
        self.assertEqual(entries["u-8"]["status"], "still_isolated")
        self.assertEqual(entries["u-8"]["upload"], "blocked")

    def test_pending_recheck_is_recheck_gap(self):
        entries = by_target(registry_from(BASE_ASSETS, BASE_ISOLATION,
                                          BASE_VERIFICATION))
        self.assertEqual(entries["u-6"]["evidence_gap"], GAP_RECHECK)

    def test_isolation_end_conditions_are_recorded(self):
        entries = by_target(registry_from(BASE_ASSETS, BASE_ISOLATION,
                                          BASE_VERIFICATION))
        self.assertEqual(entries["u-6"]["policy"]["end_condition"],
                         {"publish_requires": "fw-b"})


class CliTests(unittest.TestCase):
    def test_cli_emits_report_and_works_in_isolated_mode(self):
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "review.py"),
             "--records", str(ROOT / "records")],
            capture_output=True, text=True, check=True)
        report = json.loads(proc.stdout)
        self.assertIn("buckets", report)
        self.assertEqual(report["buckets"]["pending_execution"], ["u-9"])

    def test_cli_only_evidenced_filter(self):
        proc = subprocess.run(
            [sys.executable, "-I", str(ROOT / "review.py"),
             "--records", str(ROOT / "records"), "--only-evidenced"],
            capture_output=True, text=True, check=True)
        report = json.loads(proc.stdout)
        self.assertEqual(sum(report["buckets"].values(), []), ["u-9"])


if __name__ == "__main__":
    unittest.main()
