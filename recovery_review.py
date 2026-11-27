"""按合成登记核对隔离范围、证据时效和分批恢复。

本模块只读取本地资料并给出报告，不连接、不控制任何现场设备。
时间以记录中的观察日 ``observed`` 为准；``arrived`` 只说明回报先后，
不能用来让迟报的旧通过覆盖较新的异常观察。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from records_view import load_sources

SOURCE_NAMES = ("assets", "isolation", "verification")

REMOVED_STATES = {"removed", "absent", "decommissioned", "retired"}
PASS_RESULTS = {"verified", "pass", "ok", "cleared"}
PENDING_RESULTS = {"pending", "unknown", "unchecked", "not_reported"}
FAIL_RESULTS = {"failed", "anomaly", "mismatch", "denied"}
KIND_RANK = {"pass": 0, "pending": 1, "fail": 2}

MISSING_ASSET = "asset_not_identified"
MISSING_RECHECK = "recheck_not_completed"
MISSING_EXECUTION = "execution_report_unknown"


def _required(sources):
    missing = [name for name in SOURCE_NAMES if name not in sources]
    if missing:
        raise ValueError(f"缺少来源: {', '.join(missing)}")


def _text(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{row.get('id', '记录')} 的 {key} 需要非空文本")
    return value


def _day(row, key):
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{row.get('id', '记录')} 的 {key} 需要整数天")
    return value


def _targets(row):
    values = row.get("targets")
    if not isinstance(values, list) or not values:
        raise ValueError(f"{row.get('id', '记录')} 的 targets 需要非空数组")
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f"{row.get('id', '记录')} 的 targets 需要设备号")
    if len(values) != len(set(values)):
        raise ValueError(f"{row.get('id', '记录')} 的 targets 重复")
    return list(values)


def _result_kind(value, record):
    text = str(value).strip().lower()
    if text in PASS_RESULTS:
        return "pass"
    if text in PENDING_RESULTS:
        return "pending"
    if text in FAIL_RESULTS:
        return "fail"
    raise ValueError(f"{record} 的 result 无法识别")


def _index_assets(assets):
    units = {}
    replacements = []
    for row in assets:
        if "id_seen" in row:
            device = _text(row, "id_seen")
            if device in units:
                raise ValueError(f"资产记录重复描述 {device}")
            units[device] = row
        elif "from" in row and "to" in row:
            replacements.append(row)
        else:
            raise ValueError(f"{row['id']} 不是可用的资产记录")
    return units, replacements


def _index_verifications(verifications):
    by_target = {}
    for row in verifications:
        target = _text(row, "target")
        kind = _result_kind(row.get("result"), row["id"])
        normalized = dict(row)
        normalized["_kind"] = kind
        normalized["_observed"] = _day(row, "observed")
        normalized["_arrived"] = _day(row, "arrived")
        by_target.setdefault(target, []).append(normalized)
    return by_target


def _replacement_info(row, units, by_target):
    old, new = _text(row, "from"), _text(row, "to")
    revision = row.get("revision")
    site = row.get("site")
    day = _day(row, "day")
    issues = []

    old_unit = units.get(old)
    if old_unit is None:
        old_visibility = "unknown"
        old_visible = None
    elif str(old_unit.get("state", "")).lower() in REMOVED_STATES:
        old_visibility = "removed"
        old_visible = False
    else:
        old_visibility = "visible"
        old_visible = True

    # 更换日之后仍看到旧设备，说明它还在观察点，不能仅凭申请解绑。
    if day is not None:
        after = [record for record in by_target.get(old, [])
                 if record["_observed"] is not None and record["_observed"] >= day]
        if after:
            old_visibility = "observed_after_replacement"
            old_visible = True
            issues.append("old_device_still_visible")
    if old_visible is None:
        issues.append("old_visibility_unknown")

    identity_records = []
    new_unit = units.get(new)
    asset_conflict = False
    if new_unit is not None:
        if site is not None and new_unit.get("site") not in (None, site):
            asset_conflict = True
            issues.append("new_device_site_mismatch")
        if revision is not None and new_unit.get("revision") not in (None, revision):
            asset_conflict = True
            issues.append("new_device_revision_mismatch")
        if asset_conflict:
            # 登记归属与申请相互矛盾时，不能凭检查记录自行认定新设备。
            issues.append("new_device_identity_conflict")
        else:
            identity_records.append(new_unit["id"])

    for record in by_target.get(new, []):
        observed = record["_observed"]
        if revision is not None and record.get("revision") != revision:
            continue
        if day is not None and observed is not None and observed < day:
            continue
        identity_records.append(record["id"])

    new_identified = bool(identity_records) and not asset_conflict
    if not new_identified:
        issues.append(MISSING_ASSET)

    return {
        "from": old,
        "to": new,
        "revision": revision,
        "site": site,
        "day": day,
        "old_visibility": old_visibility,
        "old_visible": old_visible,
        "new_identified": new_identified,
        "asset_conflict": asset_conflict,
        "identity_evidence": identity_records,
        "confirmed": old_visible is False and new_identified,
        "issues": sorted(set(issues)),
    }


def _pick_current(records):
    """按观察时间选当前证据；同一天异常优先，回报时间不改变观察先后。"""
    observed = [record for record in records if record["_observed"] is not None]
    if observed:
        newest = max(record["_observed"] for record in observed)
        top = [record for record in observed if record["_observed"] == newest]
        current = max(
            top,
            key=lambda record: (
                KIND_RANK[record["_kind"]],
                record["_arrived"] if record["_arrived"] is not None else -1,
            ),
        )
        older = [record for record in records if record is not current]
        superseded = []
        for record in older:
            if record["_observed"] is None:
                reason = "no_observation"
            elif record["_observed"] == newest:
                reason = "same_observation_conflict"
            else:
                reason = "older_observation"
            superseded.append({"record": record["id"], "reason": reason,
                               "observed": record["_observed"],
                               "arrived": record["_arrived"]})
        return current, superseded

    if records:
        current = max(
            records,
            key=lambda record: record["_arrived"] if record["_arrived"] is not None else -1,
        )
        superseded = [{"record": record["id"], "reason": "no_observation",
                       "arrived": record["_arrived"]}
                      for record in records if record is not current]
        return current, superseded
    return None, []


def _evidence(records, cutoff, required_revisions):
    discarded = []
    live = []
    for record in records:
        observed = record["_observed"]
        if cutoff is not None and observed is not None and observed < cutoff:
            discarded.append({
                "record": record["id"],
                "reason": "predates_replacement",
                "observed": observed,
                "arrived": record["_arrived"],
            })
        else:
            live.append(record)

    current, superseded = _pick_current(live)
    if current is None:
        return {
            "records": [record["id"] for record in records],
            "current": None,
            "current_result": None,
            "current_revision": None,
            "observed": None,
            "accepted": [],
            "superseded": superseded,
            "discarded": discarded,
            "compliant": False,
        }

    revision_ok = (not required_revisions
                   or current.get("revision") in required_revisions)
    # 没有观察日的记录无法说明发生在更换之后，不能作为放行依据。
    timed = current["_observed"] is not None
    compliant = current["_kind"] == "pass" and revision_ok and timed
    return {
        "records": [record["id"] for record in records],
        "current": current["id"],
        "current_result": current.get("result"),
        "current_revision": current.get("revision"),
        "observed": current["_observed"],
        "accepted": [current["id"]] if compliant else [],
        "superseded": superseded,
        "discarded": discarded,
        "compliant": compliant,
        "revision_ok": revision_ok,
        "timed": timed,
    }


def _applied_targets(plan):
    """返回 (已签收集合, 是否已知)。None 表示执行回报未知。"""
    value = plan.get("applied")
    if value is None:
        return set(), False
    if value is True:
        return set(_targets(plan)), True
    if isinstance(value, list):
        if any(not isinstance(item, str) or not item for item in value):
            raise ValueError(f"{plan['id']} 的 applied 需要设备号")
        return set(value), True
    if isinstance(value, dict):
        for key in ("accepted", "applied", "signed"):
            if key in value:
                return _applied_targets({"targets": _targets(plan), "applied": value[key]})
    raise ValueError(f"{plan['id']} 的 applied 结构无法识别")


def review(sources):
    """生成只读的范围复核、证据判断和恢复报告。"""
    _required(sources)
    units, replacement_rows = _index_assets(sources["assets"])
    by_target = _index_verifications(sources["verification"])

    replacements = [
        _replacement_info(row, units, by_target) for row in replacement_rows
    ]
    confirmed_by_from = {
        item["from"]: item for item in replacements if item["confirmed"]
    }
    replacement_by_to = {
        item["to"]: item for item in replacements if item["confirmed"]
    }
    replacement_for_device = {}
    for item in replacements:
        replacement_for_device[item["from"]] = item
        replacement_for_device[item["to"]] = item

    decisions, plans = [], []
    for row in sources["isolation"]:
        if "publish_requires" in row or "allowed" in row:
            decisions.append(row)
        elif "intent" in row or "approved" in row or "applied" in row:
            plans.append(row)
        else:
            raise ValueError(f"{row['id']} 不是可用的隔离或恢复记录")

    policy = {}
    groups = []
    scope_reviews = []
    for decision in decisions:
        targets = _targets(decision)
        allowed = decision.get("allowed") or []
        if not isinstance(allowed, list) or any(not isinstance(use, str)
                                                for use in allowed):
            raise ValueError(f"{decision['id']} 的 allowed 需要用途数组")
        revisions = set()
        if decision.get("publish_requires") is not None:
            revisions.add(_text(decision, "publish_requires"))

        effective, transferred = [], []
        for target in targets:
            replacement = confirmed_by_from.get(target)
            device = replacement["to"] if replacement else target
            if replacement:
                transferred.append({"from": target, "to": device})
            effective.append(device)
            entry = policy.setdefault(device, {
                "required_revisions": set(),
                "allowed": set(),
                "sources": [],
            })
            entry["required_revisions"].update(revisions)
            entry["allowed"].update(allowed)
            entry["sources"].append(decision["id"])

        groups.append({"decision": decision["id"], "members": effective})
        scope_reviews.append({
            "isolation": decision["id"],
            "targets": targets,
            "effective_targets": effective,
            "transferred": transferred,
            "retained": [device for device in effective
                         if device not in {item["to"] for item in transferred}],
            "allowed": list(allowed),
            "publish_requires": decision.get("publish_requires"),
        })

    # 只用更换证明确认过的对应关系转移隔离；同网段/同转接点的邻居不在其中。
    devices = set(units) | set(by_target) | set(policy)
    for plan in plans:
        devices.update(_targets(plan))
    for item in replacements:
        devices.update((item["from"], item["to"]))

    evidence_by_device = {}
    for device in devices:
        replacement = replacement_for_device.get(device)
        cutoff = replacement["day"] if replacement else None
        required = policy.get(device, {}).get("required_revisions", set())
        if not required and device in replacement_by_to:
            revision = replacement_by_to[device]["revision"]
            if revision is not None:
                required = {revision}
        evidence_by_device[device] = _evidence(
            by_target.get(device, []), cutoff, required
        )

    # 归属以资产登记为基础；更换证明只把隔离对应到确认过的新设备。
    # 登记与申请矛盾的设备保留为“资产未认清”，不能凭检查记录自行认定。
    identity = {device: True for device in units}
    for item in replacements:
        if item["new_identified"]:
            identity[item["to"]] = True
        elif item["asset_conflict"]:
            identity[item["to"]] = False

    identified = {device for device in devices if identity.get(device, False)}
    retired = {device for device, unit in units.items()
               if str(unit.get("state", "")).lower() in REMOVED_STATES}
    passing = {
        device for device in devices
        if device not in retired and evidence_by_device[device]["compliant"]
    }
    eligible = passing & identified

    plan_reports = []
    approved_in_scope = set()
    planned_targets = set()
    applied_any = set()
    pending_execution = set()
    execution_unknown = set()
    for plan in plans:
        targets = set(_targets(plan))
        planned_targets.update(targets)
        approved = plan.get("approved") is True
        in_scope = targets & eligible
        out_of_scope = targets - eligible
        applied, applied_known = _applied_targets(plan)
        issues = []
        if out_of_scope:
            issues.append("out_of_scope_targets")
        unknown_applied = applied - targets
        if unknown_applied:
            issues.append("applied_outside_plan")
        if applied and not approved:
            issues.append("execution_without_approval")
        unresolved = (in_scope - applied) if approved else set()

        if approved:
            approved_in_scope.update(in_scope)
            applied_any.update(applied & in_scope)
            pending_execution.update(unresolved)
            if not applied_known:
                execution_unknown.update(unresolved)

        if not applied_known:
            execution_report = "unknown"
        elif not applied:
            execution_report = "none_reported"
        elif unresolved:
            execution_report = "partial"
        else:
            execution_report = "complete"

        plan_reports.append({
            "plan": plan["id"],
            "intent": plan.get("intent"),
            "approved": approved,
            "targets": sorted(targets),
            "eligible": sorted(in_scope),
            "out_of_scope": sorted(out_of_scope),
            "applied": sorted(applied),
            "execution_report": execution_report,
            "unresolved": sorted(unresolved),
            "partial": bool(applied & in_scope and unresolved),
            "issues": sorted(set(issues)),
        })

    partial_group = set()
    for group in groups:
        active = [device for device in group["members"] if device not in retired]
        if any(device not in passing for device in active):
            partial_group.update(active)

    device_reports = []
    missing = {MISSING_ASSET: set(), MISSING_RECHECK: set(),
               MISSING_EXECUTION: set()}
    for device in sorted(devices):
        entry = policy.get(device, {})
        evidence = evidence_by_device[device]
        approved_plan = device in approved_in_scope
        is_retired = device in retired
        is_identified = device in identified
        is_passing = device in passing

        if is_retired:
            disposition = "retired"
        elif not is_identified:
            disposition = "unidentified"
        elif device in applied_any:
            disposition = "recovered"
        elif is_passing and approved_plan:
            disposition = ("limited_recovery" if device in partial_group
                           else "ready_for_execution")
        elif is_passing:
            disposition = "ready_for_approval"
        elif device in policy:
            disposition = "isolated"
        elif device in planned_targets:
            disposition = "awaiting_evidence"
        else:
            disposition = "unknown"

        blockers = []
        if not is_retired and not is_identified:
            missing[MISSING_ASSET].add(device)
        if (not is_retired and is_identified
                and (device in policy or device in planned_targets)):
            current = evidence["current"]
            if current is None or evidence["current_result"] is None:
                blockers.append("recheck_missing")
            else:
                kind = _result_kind(evidence["current_result"],
                                    evidence["current"])
                if kind == "pending":
                    blockers.append("recheck_pending")
                elif kind == "fail":
                    blockers.append("recheck_failed")
            if evidence.get("revision_ok") is False:
                blockers.append("revision_mismatch")
            if evidence.get("timed") is False:
                blockers.append("observation_time_missing")
            if not is_passing:
                missing[MISSING_RECHECK].add(device)
        if device in execution_unknown:
            missing[MISSING_EXECUTION].add(device)

        replacement = replacement_for_device.get(device)
        superseded_by = (replacement["to"]
                         if replacement and device == replacement["from"]
                         and replacement["confirmed"] else None)

        device_reports.append({
            "device": device,
            "disposition": disposition,
            "identified": is_identified,
            "retired": is_retired,
            "under_isolation": device in policy and not is_retired,
            "requirement": {
                "revisions": sorted(entry.get("required_revisions", set())),
                "allowed": sorted(entry.get("allowed", set())),
                "sources": sorted(set(entry.get("sources", []))),
            },
            "observed": {
                "record": evidence["current"],
                "result": evidence["current_result"],
                "revision": evidence["current_revision"],
                "observed": evidence["observed"],
            },
            "evidence": {
                "records": evidence["records"],
                "accepted": evidence["accepted"],
                "superseded": evidence["superseded"],
                "discarded": evidence["discarded"],
            },
            "missing": sorted(
                name for name in (MISSING_ASSET, MISSING_RECHECK, MISSING_EXECUTION)
                if device in missing[name]
            ),
            "blockers": sorted(set(blockers)),
            "superseded_by": superseded_by,
            "recheckable": (is_identified and not is_retired
                            and bool(evidence["records"])),
            "plans": sorted(plan["plan"] for plan in plan_reports
                            if device in plan["targets"]),
        })

    by_device = {item["device"]: item for item in device_reports}
    desk = {
        "still_isolated": sorted(
            device for device, item in by_device.items()
            if item["disposition"] == "isolated"),
        "limited_recovery": sorted(
            device for device, item in by_device.items()
            if item["disposition"] == "limited_recovery"),
        "pending_execution": sorted(pending_execution),
        "awaiting_evidence": sorted(
            device for device, item in by_device.items()
            if item["disposition"] == "awaiting_evidence"),
        "ready_for_execution": sorted(
            device for device, item in by_device.items()
            if item["disposition"] == "ready_for_execution"),
        "recovered": sorted(
            device for device, item in by_device.items()
            if item["disposition"] == "recovered"),
        "retired": sorted(
            device for device, item in by_device.items()
            if item["disposition"] == "retired"),
        "unidentified": sorted(
            device for device, item in by_device.items()
            if item["disposition"] == "unidentified"),
    }
    missing_evidence = {name: sorted(values) for name, values in missing.items()}
    recheck_candidates = sorted(
        item["device"] for item in device_reports if item["recheckable"]
    )

    return {
        "replacements": sorted(replacements, key=lambda item: (item["from"], item["to"])),
        "scope_reviews": scope_reviews,
        "plans": plan_reports,
        "devices": device_reports,
        "desk": desk,
        "missing_evidence": missing_evidence,
        "recheck_candidates": recheck_candidates,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path,
                        default=Path(__file__).with_name("records"))
    parser.add_argument("--device", help="只显示指定设备的报告")
    parser.add_argument("--recheck-candidates", action="store_true",
                        help="只列出有证据支持、可再复核的设备")
    args = parser.parse_args()
    try:
        result = review(load_sources(args.records))
        if args.recheck_candidates:
            result = {"recheck_candidates": result["recheck_candidates"]}
        elif args.device:
            found = [item for item in result["devices"]
                     if item["device"] == args.device]
            if not found:
                raise ValueError("设备不存在")
            result = found[0]
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
