# 运行与恢复

## 启动

```text
python3 <技能>/scripts/bf/front.py launch --base <任务>/.batch-flow [--model <前端模型>]
```

前端会话在后台运行 `bf run`，每 10 分钟用 `bf report` 汇报：在途工单与耗时、最近完成和新开立的工单、待回答的问题、额度余量。

也可以不用前端，直接运行：

```text
python3 <技能>/scripts/bf/cli.py --base <任务>/.batch-flow run --profile codex=<profile> --profile claude=<profile> [--parallel N]
```

## 回答问题

`attention.md` 列出待回答的问题。把用户原话写入 `<任务>/.batch-flow/answers/<n>.txt`，然后：

```text
python3 <技能>/scripts/bf/cli.py --base <任务>/.batch-flow answer WO-0012 --file answers/<n>.txt --approve|--reject
```

只有依赖该问题的工单会等待，其余工作照常推进。

## 停止、恢复、重启

- `stop`：不再派发新工单，已在运行的调用结束后照常回收。`resume` 恢复。
- 驱动器随时可以重启：状态全部在文件里；上次未回收的调用会被接管或登记为无效（免扣）后重派。
- CLI 工具失败（无结果、认证错误）登记为无效、免扣；同一工单连续失败达到上限会转技术负责人诊断。
- profile 缺失时 `run` 自动 probe（不调用模型），见 [CLI 适配器](cli-adapter.md)。
