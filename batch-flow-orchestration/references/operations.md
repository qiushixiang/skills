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

## 调整

用户随时可以调整，每条都带 `--approval <base>/answers/<n>.txt`（用户原话，一次调整 = 一次修订 + 一条事件）。命令均为 `bf/cli.py --base <任务>/.batch-flow ...`：

- `models <后端>`：只读，查询模型目录；`set-role <角色> --backend B --name '<说法>' [--effort E]`：换角色模型，见 [CLI 适配器](cli-adapter.md)。
- `envelope --add|--remove <批次>...`：自动推进范围；`grant <交付目标> --implement|--backup|--lead|--repair|--review N`：只增不减。
- `hold <交付目标或批次>...` / `unhold ...`：只暂停这一部分；`supersede WO-n --reason R`：作废开立中的工单。
- 新增或改写交付目标属于计划修订，由技术负责人起草（`revise`），聊天前端不运行 `init`/`revise`。

## 运维（聊天前端）

- `report --text`：给人看的中文进展（正在做、下一步、待你决定、角色、批次），不含工单号；`report` 输出完整 JSON（含 roles、next、batches、ops）。
- 同一工单连续 CLI 不可用达到 `invalid_retry` 次，工单被标记 `ops` 并暂停（其余照常推进），不再自动开裁决。前端读故障原文后：`retry WO-n --note T`（如切换 codex 账号后重试，免扣额度）、`escalate WO-n --note T`（交技术负责人，trigger `tool_failure`），或告诉用户。
- 前端可读任何文件、运行只读命令和已配置的账号切换命令；写入只限 `answers/` 下的新文件；不改代码、不做技术判断。

## 通知、体检与立即停止

- `watch [--since N] [--once]`：把值得关注的事件（驱动器启停、待你决定、运维暂停、交付结果、批次放行、停止）逐条输出为一行中文；前端用 Monitor 挂着它。`report` 的 `driver.alive` 表示驱动器是否在运行。
- `doctor`：列出任务目录里引用了、但技能中已不存在的脚本（`{"missing": [{file, line, ref}]}`）；切换或升级技能前运行。
- `stop` 同时阻止续跑（不合法交付暂存，`resume` 后继续）；`stop --now` 立即按进程组终止在途调用，登记为无效、免扣，驱动器退出。
