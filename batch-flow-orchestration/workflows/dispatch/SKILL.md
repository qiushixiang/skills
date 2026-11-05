---
name: batch-flow-dispatch
description: 按已批准配置真实派发代理，登记回执、计时和回收。
---
# 代理派发与回收

编排者遵守[红线](../../references/roles/orchestrator.md#编排者红线)：顶层 `orchestrator_todo` 是本会话动作；`subagent_payload` 是要交出的技术内容，不执行其中的 Git、构建、测试或实现指令。

**调用入口先固定**：Linux 上派发 AGY、Codex CLI、Claude Code CLI，一律按[Linux CLI 固定入口](../../references/linux-cli.md)使用 `scripts/agent_cli.py` 的 probe/run/collect；profile 同版本复用，不每次探索参数或重写调用器。原生工具按宿主只读一份：[AGY 与 Codex](../../references/hosts-agy-codex.md)、[Claude Code](../../references/claude-code.md)。正式台账登记、授权、预算和计时仍按下述流程执行。

Claude Code 或 AGY 担任主编排者时，一律经各自受限启动器并[只运行 next](../../references/roles/orchestrator.md#派发只用-next两宿主相同)：卡、调用、回执、计时与回收登记由 `dispatch_card.py next` 完成，下述登记规则由它机械执行。AGY 角色 tool 配 `agy-native` 时 next 给出原生 define_subagent/invoke_subagent（model 写 inherit 或与编排者相同则继承主会话模型），配 agy/codex/claude-cli 时给出固定 CLI 入口，见 [AGY 宿主](../../references/hosts-agy-codex.md#agy-编排者linux-受限启动)。

提示词经统一派发卡自动注入项目/当前节点的目标、验收项与非目标，规则见[节点目标的结构化传递](../../references/protocol-plan.md#节点目标的结构化传递)；原生工具接收完整 `subagent_payload`，不能只摘 instruction 或重写成“关闭以下问题”。

当前 `dispatch_owner` 核对授权、实际模型/工具/effort、版本、预算与上下文；先 start，再真实调用宿主工具。首派给完整 task.instruction 和已有设计书定位（批准的小任务可无设计书），续接给生效差异，换人/兜底给完整当前指令。运行句柄、不可变原始回执、原生 timer ID 或脚本 ticket 到手后 register_dispatch；快速结束直接 finish。重复派发写任务前先核实原句柄。

长任务每 600 秒汇总在途目标与可核查进展。优先宿主实际可用且能唤醒负责会话的原生一次性 schedule；否则用脚本计时，通知不可靠时主动短轮询。到期重设、结束取消；切换先核实旧触发器，不能双重计时。细节见[协作指南](../../references/agent-collaboration.md)和[宿主接入](../host-setup/SKILL.md)。

权限拒绝、空回复或未知退出保留原始结果，核实无同一写任务运行，再按有效投入分类；不静默换模型、盲重试或重置预算。交还控制权前执行 pre_yield，用户停止优先。
