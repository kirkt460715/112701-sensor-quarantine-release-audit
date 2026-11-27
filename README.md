# 生态传感组的网络隔离解除复核

编号和网段标签仅为合成数据，无接入地址或控制命令。 资料均为本地合成记录。

以 Python 3.11.2 运行，无第三方依赖。`records/` 中的 `assets`、`isolation`、`verification` 对应独立来源。每份材料是 JSON 对象数组，`id` 在同一来源内唯一。报告和观察的时间以各原始记录给出的字段为准。

```sh
python3 -m unittest -v
python3 records_view.py
python3 recovery_review.py
python3 recovery_review.py --device u-9
python3 recovery_review.py --recheck-candidates
```

资料入口列出来源、记录标识及条数。通过 `--source` 指定来源，`--offset` 和 `--limit` 限定读取的位置与数量。`--records` 可指定同样结构的其他资料目录。

`recovery_review.py` 只读本地资料并给出报告，不连接、不控制现场设备。

- 更换证明只有在旧设备已不在观察点、且新设备能从登记或检查记录中确定归属时才成立；成立时隔离只对应到新设备，同组其他设备不因共用转接点而解绑。
- 证据以 `observed` 的先后为准，`arrived` 只记录回报顺序；迟报的旧通过不会覆盖较新的异常观察，早于更换日的记录不用于放行。没有观察日的记录不能作为放行依据。
- 只有具备通过证据、版本符合要求且已获核准的设备才进入恢复范围；其余目标留在未决范围。执行回报为 `null` 表示执行结果未知，部分签收时只记录已签收部分，未签收部分继续待执行。
- `missing` 把缺口分为 `asset_not_identified`（资产未认清）、`recheck_not_completed`（复验未完成）和 `execution_report_unknown`（执行回报未知）。`desk` 列出仍隔离、有限恢复和待执行等状态的设备；`--recheck-candidates` 只列出有证据支持、可再复核的设备。诊断用途保留在隔离策略的 `allowed` 中。
