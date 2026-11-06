---
name: batch-flow-orchestration
description: 以下任一情形加载：用户要求使用本技能（batch-flow / 分批编排 / 任务分流编排）；本会话是 batch-flow 编排者或角色会话（经 start-claude-orchestrator / start_agy_orchestrator 启动、启动提示自称 batch-flow 编排者，或收到 .batch-flow/records/dispatch 派发卡）；要读取、推进或登记某任务的 .batch-flow/flow.json 台账（状态、派发、裁决、确认、计划更新）。与台账无关的普通开发、审阅或问答不加载。按风险与未决问题决定直派闭环或分批推进；技术负责人负责方案与独立复核，编排者协调跨批，脚本维护版本、证据和恢复门禁。
---

# 任务分流与分批编排

项目编排者立即遵守[编排者红线](references/roles/orchestrator.md#编排者红线)：技术 payload 只派发，不在主会话代做。用户授权维护本技能时仍按下述维护例外执行。

项目任务先绑定用户指定的任务 base（当前具体任务目录），正式台账默认是 `<任务base>/.batch-flow/flow.json`。同一源码项目可有多个任务；未指定当前任务时先询问，不按源码工作目录、更新时间或任意候选推定，不自动搜索或切换任务。任务目录和源码工作目录分别明确；AGY 与 Claude Code 启动器用 `--task-base` 传入，其他宿主在本次任务上下文明确并始终显式使用对应 `--store`。

本技能用于项目任务；维护本技能本身时按用户授权的维护任务执行，不递归启动项目编排。新任务先由技术负责人核查目标、范围、验收与依赖，判断局部直派或分批。分批计划由用户指定的技术负责人编制，用户审阅通过后才 init，并保存审阅通过原话供 authorize 引用；编排者发现缺正式台账、批次设计或审阅通过记录时，请用户补齐，不自行派规划。已有批准计划中的正常节点按计划继续，不重复分流。用户未指定主会话角色时，主会话担任编排者；已派发的子会话保持派发身份。

**第一证据是实际实现代码，以及针对验收目标、可重复运行且实际通过的构建/测试。** 独立审阅、固定版本、真实模型及调用记录、预算不重置、用户授权边界与停止指令优先适用于两种路径。技能和 DONE 状态不额外授权提交、部署或发布。正式 `.batch-flow/flow.json` 只通过 `scripts/flow.py` 修改。

按当前身份只读对应角色文档：[编排者](references/roles/orchestrator.md)、[技术负责人](references/roles/technical-lead.md)、[执行者](references/roles/executor.md)、[第三方](references/roles/third-party.md)。程序中的 `architect` 是技术负责人的规划/裁决职责，`reviewer` 是其独立审阅职责；兼任时必须隔离作者与审阅上下文。角色文档管职责，以下子技能管一段流程；嵌套 SKILL.md 需由入口显式打开，不假设宿主自动发现。

按需加载一段流程：

| 当前需要 | 子技能 |
|---|---|
| 新任务分流、局部直派及交付 | [任务分流与直派闭环](workflows/triage-direct/SKILL.md) |
| 初次方案或实质计划变更 | [计划编制与同步更新](workflows/plan-sync/SKILL.md) |
| 真实代理调用和回收 | [代理派发与回收](workflows/dispatch/SKILL.md) |
| 固定版本的独立审阅、R2 | [独立审阅与复核](workflows/review/SKILL.md) |
| 严重阻塞、环境失败、预算与恢复 | [阻塞诊断与恢复](workflows/recovery/SKILL.md) |
| 宿主能力、原生定时和 stop hook | [宿主配置与接入验证](workflows/host-setup/SKILL.md) |

**按角色文档的“按动作加载”表只读当前动作所需文档**，不预加载全部参考。每条规则只有一处正文：字段与命令在[协议总览](references/protocol.md)及其四个分册，设计书在[设计书规范](references/artifacts.md)，证据与验证在[工程与验证范围](references/engineering.md)，上下文隔离在[上下文规则](references/contexts.md)，计时与统计在[追踪](references/tracking.md)，派发信封在[提示词规范](references/prompts.md)，通用派发与汇报在[协作指南](references/agent-collaboration.md)，宿主调用形态在 [Claude Code 宿主](references/claude-code.md) 或 [AGY 与 Codex 宿主](references/hosts-agy-codex.md)（只读本宿主一份）。其他文档只链接，不复述。

技术规划、实现和审阅共同遵守[工程与验证范围](references/engineering.md)；预算接管和用户离开授权按[恢复流程](workflows/recovery/SKILL.md)所链接的协议处理。审阅额度必须满足[程序强制的配套规则](references/protocol-budget.md#审阅预算配套脚本门禁)，不能先用备用/兜底返修再申请复核额度。脚本变更运行 `scripts/test_flow.py`、`scripts/test_review_regressions.py`、`scripts/test_review_budget.py`；hook、定时器分别运行 `scripts/test_stop_hook.py`（Claude Code 编排者另加 `scripts/test_orchestrator_guard.py`、`scripts/test_dispatch_card.py`、`scripts/test_claude_orchestrator.py`）、`scripts/test_progress_timer.py`，文档只校验结构、链接与命令，不重跑无关产品构建。

模型来源必须是本任务用户指定或其明确授权选择，禁止从旧任务、示例或计划字段静默推定；只在派发已安排角色前补足缺项，不要求用户重新指定所有角色。编排者不探索业务源码、不写技术方案；同一任务同一时刻只有一个 `dispatch_owner`。局部任务由技术负责人实际派发和回收，跨批协调由编排者承担。真实派发回执与进度通知由当前责任人登记；`pre_yield`、宿主 stop hook 与用户停止优先规则见对应子技能。

Linux CLI 派发的固定脚本与维护测试见[统一调用入口](references/linux-cli.md)；由派发子技能直接加载，不要求每轮重新探索 CLI 用法。

AGY 或 Claude Code 启动器会话忘记 profile 时，通过绑定台账的 `flow.py capabilities` 或 status 的 host_capabilities 恢复；不得将丢失上下文当作权限缺失。维护此接入运行 `scripts/test_host_capabilities.py`、`scripts/test_agy_orchestrator.py`。
