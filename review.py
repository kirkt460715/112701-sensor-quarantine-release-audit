"""合成登记：合并独立来源，办理防御性隔离、范围复核与分批恢复。

本模块只登记和比对本地记录，不向现场设备发送任何接入或控制指令；
恢复是否执行完全以签收记录为准，未签收的范围继续保留在隔离态。

状态口径：
- still_isolated   仍隔离：隔离有效且没有任何保留用途
- limited_recovery 有限恢复：上传仍阻断，诊断等已核准的保留用途继续有效
- pending_execution 待执行：证据支持且恢复计划已核准，尚无设备执行回报
- recovered        已恢复：执行回报已签收

证据缺口口径：
- asset_unrecognized   资产未认清：无法把设备归入本申请的隔离范围或更换证明
- recheck_incomplete   复验未完成：缺少当前维修版本下的有效验证
- execution_unreported 设备执行回报未知：恢复已核准，但没有签收回报
"""
import argparse
import json
import sys
from pathlib import Path

# 隔离模式（python -I）下脚本目录不在 sys.path 中，显式登记可信代码目录；
# 资料目录始终由参数传入，不参与模块解析。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from records_view import load_sources

STILL_ISOLATED = "still_isolated"
LIMITED_RECOVERY = "limited_recovery"
PENDING_EXECUTION = "pending_execution"
RECOVERED = "recovered"

GAP_ASSET = "asset_unrecognized"
GAP_RECHECK = "recheck_incomplete"
GAP_EXECUTION = "execution_unreported"

STATUS_LABELS = {
    STILL_ISOLATED: "仍隔离",
    LIMITED_RECOVERY: "有限恢复",
    PENDING_EXECUTION: "待执行",
    RECOVERED: "已恢复",
}

GAP_LABELS = {
    GAP_ASSET: "资产未认清",
    GAP_RECHECK: "复验未完成",
    GAP_EXECUTION: "设备执行回报未知",
}


def _split_decisions_and_plans(rows):
    decisions = [row for row in rows if "intent" not in row]
    plans = [row for row in rows if "intent" in row]
    return decisions, plans


def _scope_index(decisions):
    by_target = {}
    for decision in decisions:
        for target in decision.get("targets", []):
            by_target.setdefault(target, []).append(decision)
    return by_target


def _effective_evidence(checks, target):
    """按观察时刻取最新证据；迟报记录的到达时间不参与新旧判断。"""
    rows = [row for row in checks if row.get("target") == target]
    timed = [row for row in rows if isinstance(row.get("observed"), int)]
    if not timed:
        return None, rows
    return max(timed, key=lambda row: row["observed"]), rows


def _stale_late_passes(rows):
    """观察时刻更早、却延迟送达的旧通过。

    新旧只以 observed（观察时刻）为准，arrived（到达时刻）仅用于识别迟报：
    新的异常/待复验证据不会被迟到的旧通过覆盖，单条旧通过迟报同样留痕。
    """
    if not [row for row in rows if isinstance(row.get("observed"), int)]:
        return []
    stale = []
    for row in rows:
        arrived = row.get("arrived")
        if (row.get("result") == "verified"
                and isinstance(row.get("observed"), int)
                and isinstance(arrived, int)
                and row["observed"] < arrived):
            stale.append(row["id"])
    return stale


def _build_entry(target, seen, proof_to, scope_by_target, plans, checks):
    asset = seen.get(target)
    proofs = proof_to.get(target, [])

    replacement = None
    succeeded_by = None
    decisions = list(scope_by_target.get(target, []))
    for proof in proofs:
        old = seen.get(proof["from"])
        old_still_visible = bool(old) and old.get("state") != "removed"
        attributable = (proof["from"] in scope_by_target
                        and old is not None and not old_still_visible)
        replacement = {
            "proof_id": proof["id"],
            "from": proof["from"],
            "to": proof["to"],
            "day": proof.get("day"),
            "old_still_visible": old_still_visible,
            "attributable": attributable,
        }
        # 仅当新设备可确定归属本申请时，才承接旧设备的隔离范围与保留用途
        if attributable:
            decisions.extend(scope_by_target.get(proof["from"], []))

    for proof in proof_to.values():
        for row in proof:
            if row["from"] == target:
                succeeded_by = row["to"]

    decision_id = None
    required_revision = None
    retained_uses = []
    for decision in decisions:
        decision_id = decision_id or decision["id"]
        if decision.get("publish_requires"):
            required_revision = decision["publish_requires"]
        retained_uses.extend(decision.get("allowed", []))
    retained_uses = sorted(set(retained_uses))

    latest, all_checks = _effective_evidence(checks, target)
    stale_late_passes = _stale_late_passes(all_checks)

    if asset and asset.get("revision"):
        observed_revision = asset["revision"]
    elif latest and latest.get("revision"):
        observed_revision = latest["revision"]
    elif proofs and proofs[0].get("revision"):
        observed_revision = proofs[0]["revision"]
    else:
        observed_revision = None

    in_scope = bool(decisions)
    evidence_qualifies = bool(
        required_revision
        and latest is not None
        and latest.get("result") == "verified"
        and latest.get("revision") == required_revision
        and (replacement is None or replacement["attributable"])
    )

    reasons = []
    if replacement is not None and replacement["old_still_visible"]:
        reasons.append("old_device_still_visible")
    if replacement is not None and not replacement["attributable"]:
        reasons.append("replacement_not_attributable")
    if latest is None:
        reasons.append("no_evidence")
    elif latest.get("result") != "verified":
        reasons.append("evidence_not_verified")
    elif required_revision and latest.get("revision") != required_revision:
        reasons.append("evidence_revision_mismatch")

    evidence_gap = None
    if not in_scope and replacement is None:
        evidence_gap = GAP_ASSET
    elif replacement is not None and not replacement["attributable"]:
        evidence_gap = GAP_ASSET
    elif not evidence_qualifies:
        evidence_gap = GAP_RECHECK

    plan_row = next((row for row in plans if target in row.get("targets", [])), None)
    plan_in_scope = in_scope or (replacement is not None and replacement["attributable"])
    plan = None
    if plan_row is not None:
        plan = {
            "id": plan_row["id"],
            "intent": plan_row.get("intent"),
            "approved": bool(plan_row.get("approved")),
            "applied": plan_row.get("applied"),
        }
        if not plan_in_scope:
            reasons.append("plan_target_out_of_scope")

    status = LIMITED_RECOVERY if retained_uses else STILL_ISOLATED
    upload = "blocked"
    if plan is not None and plan["approved"] and evidence_qualifies and plan_in_scope:
        applied = plan["applied"]
        if isinstance(applied, list) and target in applied:
            status = RECOVERED
            upload = "active"
        else:
            # 未签收或部分签收：未决范围继续挂起，系统不代现场执行
            status = PENDING_EXECUTION
            upload = "approved_pending"
            if evidence_gap is None:
                evidence_gap = GAP_EXECUTION

    return {
        "target": target,
        "site": asset.get("site") if asset else None,
        "zone": asset.get("zone") if asset else None,
        "scope": {
            "decision_id": decision_id,
            "in_scope": in_scope or plan_in_scope,
            "via_replacement": replacement["from"] if replacement else None,
        },
        "policy": {
            "required_revision": required_revision,
            "retained_uses": retained_uses,
            "end_condition": (
                {"publish_requires": required_revision}
                if required_revision else None
            ),
        },
        "observed": {
            "revision": observed_revision,
            "asset_state": asset.get("state") if asset else None,
            "succeeded_by": succeeded_by,
        },
        "replacement": replacement,
        "evidence": {
            "latest": latest["id"] if latest else None,
            "result": latest.get("result") if latest else None,
            "revision": latest.get("revision") if latest else None,
            "stale_late_passes": stale_late_passes,
        },
        "evidence_qualifies": evidence_qualifies,
        "evidence_gap": evidence_gap,
        "plan": plan,
        "plan_in_scope": plan_in_scope,
        "retained_uses": retained_uses,
        "upload": upload,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reasons": reasons,
    }


def build_registry(sources):
    """把三个独立来源合成为按设备登记的复核记录。"""
    assets = sources.get("assets", [])
    decisions, plans = _split_decisions_and_plans(sources.get("isolation", []))
    checks = sources.get("verification", [])

    seen = {row["id_seen"]: row for row in assets if row.get("id_seen")}
    replacements = [row for row in assets if row.get("from") and row.get("to")]
    scope_by_target = _scope_index(decisions)

    proof_to = {}
    for row in replacements:
        proof_to.setdefault(row["to"], []).append(row)

    targets = set(scope_by_target)
    for row in replacements:
        targets.add(row["to"])
    for plan in plans:
        targets.update(plan.get("targets", []))

    return [
        _build_entry(target, seen, proof_to, scope_by_target, plans, checks)
        for target in sorted(targets)
    ]


def evidence_backed(sources):
    """只挑出有当前版本有效证据支持、可再复核的设备。"""
    return [entry for entry in build_registry(sources) if entry["evidence_qualifies"]]


def policy_vs_observed(entries):
    """逐台比较策略当前要求与实际可观察状态。"""
    return [
        {
            "target": entry["target"],
            "required_revision": entry["policy"]["required_revision"],
            "observed_revision": entry["observed"]["revision"],
            "revision_match": (
                entry["policy"]["required_revision"] is not None
                and entry["policy"]["required_revision"]
                == entry["observed"]["revision"]
            ),
        }
        for entry in entries
    ]


def console_report(sources, only_evidenced=False):
    """值班台视图：各状态桶、策略与观察比对、完整登记条目。"""
    entries = build_registry(sources)
    if only_evidenced:
        entries = [entry for entry in entries if entry["evidence_qualifies"]]
    buckets = {
        STILL_ISOLATED: [],
        LIMITED_RECOVERY: [],
        PENDING_EXECUTION: [],
        RECOVERED: [],
    }
    for entry in entries:
        buckets[entry["status"]].append(entry["target"])
    return {
        "status_labels": STATUS_LABELS,
        "gap_labels": GAP_LABELS,
        "buckets": buckets,
        "policy_vs_observed": policy_vs_observed(entries),
        "entries": entries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path,
                        default=Path(__file__).with_name("records"))
    parser.add_argument("--only-evidenced", action="store_true",
                        help="只保留有有效证据支持、可再复核的设备")
    args = parser.parse_args()
    try:
        sources = load_sources(args.records)
        report = console_report(sources, only_evidenced=args.only_evidenced)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
