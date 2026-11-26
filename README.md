# 生态传感组的网络隔离解除复核

编号和网段标签仅为合成数据，无接入地址或控制命令。 资料均为本地合成记录。

以 Python 3.11.2 运行，无第三方依赖。`records/` 中的 `assets`、`isolation`、`verification` 对应独立来源。每份材料是 JSON 对象数组，`id` 在同一来源内唯一。报告和观察的时间以各原始记录给出的字段为准。

```sh
python3 -m unittest -v
python3 records_view.py
```

资料入口列出来源、记录标识及条数。通过 `--source` 指定来源，`--offset` 和 `--limit` 限定读取的位置与数量。`--records` 可指定同样结构的其他资料目录。
