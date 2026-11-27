# 生态传感组的网络隔离解除复核

编号和网段标签仅为合成数据，无接入地址或控制命令。 资料均为本地合成记录。

以 Python 3.11.2 运行，无第三方依赖。`records/` 中的 `assets`、`isolation`、`verification` 对应独立来源。每份材料是 JSON 对象数组，`id` 在同一来源内唯一。报告和观察的时间以各原始记录给出的字段为准。

```sh
python3 -m unittest -v
python3 records_view.py
python3 -I review.py
```

资料入口列出来源、记录标识及条数。通过 `--source` 指定来源，`--offset` 和 `--limit` 限定读取的位置与数量。`--records` 可指定同样结构的其他资料目录。

## 合成登记与复核（`review.py`）

`review.py` 把三个独立来源按设备合成为复核登记，只做记录登记与比对，不向现场设备发送任何接入或控制指令；恢复是否执行以签收记录（`applied`）为准，未签收的范围继续保留在隔离态。

```sh
python3 -I review.py                 # 全量值班台视图
python3 -I review.py --only-evidenced  # 仅保留有有效证据支持、可再复核的设备
```

状态口径（`buckets`）：

| 状态 | 含义 |
| --- | --- |
| `still_isolated` 仍隔离 | 隔离有效且没有任何保留用途 |
| `limited_recovery` 有限恢复 | 上传仍阻断，`allowed` 中已核准的保留用途（如 `diagnostic` 诊断通路）继续有效，不被错误取消 |
| `pending_execution` 待执行 | 证据支持且恢复计划已核准，但无设备执行回报；部分签收时未签收的设备留在本桶 |
| `recovered` 已恢复 | 执行回报已签收（目标出现在 `applied` 中） |

关键规则：

- **防御性隔离与范围**：隔离决定记录作用对象（`targets`）、可保留用途（`allowed`）和起止条件（如 `publish_requires` 维修版本）。同一转接点的相邻设备不会因他人解封而连带解封。
- **更换证明核对**：新设备只有在旧设备已不在观察点可见（资产 `state` 为 `removed`）且更换证明能对应到原隔离对象时才可归属本申请，承接隔离范围与保留用途；旧设备仍可见或无法归属时记为 `asset_unrecognized`（资产未认清），其恢复计划判为范围外。
- **证据与迟报**：证据新旧只按 `observed` 观察时刻判断，`arrived` 仅用于识别迟报；观察时刻更早、延迟送达的旧通过记入 `stale_late_passes`，不能覆盖新的异常或待复验状态。版本不符、无证据或结果未通过记为 `recheck_incomplete`（复验未完成）；已核准但无签收回报记为 `execution_unreported`（设备执行回报未知）。
- **分批恢复**：仅当前维修版本验证通过的设备满足 `evidence_qualifies`；`--only-evidenced` 供用户只对这部分设备再复核。`policy_vs_observed` 逐台比较策略要求版本与实际可观察版本。
