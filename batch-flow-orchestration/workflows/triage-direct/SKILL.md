---
name: batch-flow-triage-direct
description: 新任务分流，并由技术负责人闭环局部直派任务。
---
# 任务分流与直派闭环

技术负责人先核查目标、范围、验收、依赖。能局部验收且无需跨组协调或前置契约决定时，用最小任务说明（目标、范围、验收、前提、证据入口）直派；否则进入[计划编制](../plan-sync/SKILL.md)，交用户审阅，通过后登记并交编排者。已批准计划的正常节点不重复分流。

局部任务由技术负责人作为唯一 `dispatch_owner=architect` 实际 start、调用、register_dispatch、等待和回收，按[派发流程](../dispatch/SKILL.md)执行。实现交执行者，独立审阅与 R2 按[审阅流程](../review/SKILL.md)执行。主执行→备选一次→负责人有限兜底；兜底另开实现会话且不得自审。授权、模型来源和预算缺项不推定。转分批保留已有代码、证据、预算与历史。

先通过 init 登记 task_mode=local、最小任务图和规划作者 context：执行节点 role=executor，独立审阅节点 role=reviewer 且 repair_tasks 指向执行节点，放行节点 kind=acceptance/role=architect 且依赖审阅；这些局部节点均配置 dispatch_owner=architect。按[接管协议](../../references/protocol-budget.md#执行者接管)配置执行预算及备选，不省略独立审阅。未安排的角色可以不填模型；新增角色前补齐真实配置。技术负责人凭用户原始授权用 authorize --actor architect 登记局部范围，不能自行批准。start/close 显式 --actor architect，finish 用实际执行或审阅职责；恢复决定也要实际 close 后接上派发。

转分批时加载计划同步子技能，设计书交用户审阅；通过后 init 并保存审阅通过原话，告知用户台账与记录路径，由用户安排的编排者接手。运行中节点先回收，再变更其派发负责人；只有缺少授权、用户审阅或宿主能力时才明确等待。已授权的局部闭环不再等用户口头提醒继续。

局部结束执行 `pre_yield --actor architect`，交付代码、验证、审阅、未完成项和下一责任人。等待依赖或用户停止时说明原因，不把 READY、pending 或口头承诺当作实际派发。
