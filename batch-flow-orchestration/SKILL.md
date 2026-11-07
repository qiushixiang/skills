---
name: batch-flow-orchestration
description: 以下情形加载：用户要求使用本技能（batch-flow / 分批推进 / 工单驱动）；要为某任务编制或修订 batch-flow 计划（plan.json）；本会话是 batch-flow 聊天前端（经 bf/front.py launch 启动）；要查看、推进或回答某任务的 .batch-flow 工单。与工单无关的普通开发、审阅或问答不加载。
---

# batch-flow：工单驱动的分批推进

目标：用户批准计划后，大多数情况下无需人工干预，由脚本按工单派发角色（执行者、审阅者、技术负责人），角色以裁决机制互相配合，按计划目标推进到完成。用户只在四处介入：批准计划、回答问题、停止或恢复、查看汇报。

## 组成

| 部分 | 作用 |
|---|---|
| `<任务>/.batch-flow/plan.json` | 目标、交付目标、审阅组、角色模型、额度与上下文策略、环境、授权包络 |
| `orders.json` / `events.jsonl` | 工单（唯一状态）与只追加日志；只经 `bf` 脚本写入 |
| `scripts/bf/cli.py` | `init / revise / run / status / report / answer / stop / resume` |
| `scripts/start-batch-flow.sh` / `scripts/bf/front.py launch` | Claude / Codex / agy / OpenCode V2 聊天前端：后台启动和查看驱动器、每 10 分钟汇报、转交用户回答；不调度。后端能力差异见[运行与恢复](references/operations.md) |
| `scripts/agent_cli.py` | 各 CLI 后端（codex、claude、opencode、agy）的调用适配，见 [CLI 适配器](references/cli-adapter.md) |

批次设计仍保留审阅节点：计划的 `review_groups` 定义审阅范围、角色与放行批次，交付目标可依赖审阅组。运行时脚本将这些节点展开为 review、repair 等工单；工单承载执行，不替代计划中的审阅安排。

技能负责状态、角色、裁决、上下文与证据门禁。具体工程方案及验证副作用边界由技术负责人在批次设计中确定，经设计审阅和授权后实施；不在技能里维护项目日志路径或产物豁免清单。

## 对外接口（项目脚本与角色可依赖）

- `bf/cli.py --base <任务>/.batch-flow record [--cwd D] -- 命令 …`：运行命令（直接子进程）并写 `records/<随机>.json`，字段 `command`(list)、`cwd`、`started_at`、`ended_at`、`exit_code`(int)、`stdout`、`stderr`；标准输出一行 `{"ok","evidence","exit_code"}`；退出码透传（0..125，否则 1）；不改工单状态。
- `bf/cli.py --base <…> snapshot --repo R --paths 文件…`：写代码快照记录，字段 `repository`、`base_commit`、`paths`、`diff`、`untracked_base64`。
- 门禁只接受符合这两种结构的证据文件，手写文件不算证据。项目的受控入口应调用 `bf record`；切换或升级技能前运行 `bf doctor` 检查任务目录是否还引用已不存在的技能脚本。

## 按身份只读一份

| 身份 | 读 |
|---|---|
| 技术负责人：编制或修订计划 | [计划编制](references/plan.md)；按需读 [技术负责人](references/technical-lead.md)、[设计书](references/design-book.md)、[工程规范](references/engineering.md) |
| 聊天前端 | 启动提示即全部规则；需要时读 [运行与恢复](references/operations.md) |
| 执行者、审阅者、技术负责人（处理工单） | 只按收到的工单卡工作；卡内已含目标、环境、本单要做的事、约束和交付格式，并按卡的“## 规范”节读所列文件的对应章节。角色要点见 [角色](references/roles.md) |
| 维护本技能 | [设计书](design/workorder-redesign.md)；从技能仓库根运行 `python3 -m unittest discover -s batch-flow-orchestration/scripts -p 'test_*.py'`，另运行 `batch-flow-orchestration/scripts/bf/test_adapter.py`；离线测试不调用真实模型 |

## 自主推进

- 用户确认仅限[六类新增用户决定](references/user-decisions.md)。技术阻塞、次数用尽和暂时无解只暂停受影响路径并报告，不自动生成 ask_user。
- 技术轮次只用于实施、返修和审阅。裁决、分诊及批准落实属于协调，不占兜底实施额度；调用仍完整记账，并受独立协调重试和无进展上限约束。
- 计划批准时一并确定已知路径权限、验证边界与预算；已批准授权的落实不重复询问用户。新增授权必须有结构化动作，关联动作整包批准和执行。
- 派发前检查明确声明的权限需求；缺项先由脚本转协调，不花实施轮次核查状态。历史登记缺陷只经带批准原文的 reconcile 修复，保留历史运行。

## 硬规则

- 状态文件只经 `bf` 写入；不手改 plan.json、orders.json、events.jsonl。
- 计划和超出授权包络的事项（追加额度、改验收或范围）必须有用户原话文件。
- 交付是否合法、下一张工单是谁，由脚本按结构判定，不由任何会话代为判断。
- 技能本身和 DONE 状态不授权提交、推送或发布；“本地提交”须是计划中明确的交付目标。
