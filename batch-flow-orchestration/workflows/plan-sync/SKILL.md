---
name: batch-flow-plan-sync
description: 技术负责人编制并同步更新设计书、正式计划和任务指令。
---
# 计划编制与同步更新

规划边界和复杂度依据见[工程规范](../../references/engineering.md)。小任务进入直派分支，只有分批方案需要本节总体设计书。

技术负责人按[设计书规范](../../references/artifacts.md)写固定路径、稳定章节和批次编号；有效方案与待定建议分开，只改受影响部分，不写实时进度。初次规划按关联技术链路配置 `context_groups`，无关域分开，跨组契约总体协调。

分批计划由用户指定的技术负责人编制；用户审阅通过即授权该技术负责人登记计划（init、写入台账），无需再单独请示。先交设计书给用户审阅，按意见修改；用户审阅通过后，把通过原话保存为固定文件（如 `.batch-flow/user/plan_v1_approval.md`），再 `init`，同次提交 plan、simulation、作者 context、task_mode=batched 与 design 关联，形状见[初始化示例](../../references/init-example.md)；编排者 authorize 时引用该原话文件。用户授权先落盘时，角色模型可暂缺（技术负责人自己的配置除外），之后由编排者凭用户指定经 set_models 首次登记；缺模型的任务不会派发。局部小任务使用 task_mode=local，允许不带 design。实质变更先写设计书和各 `task.instruction`，再一次 `plan_update` 同时提交 plan、decision_id、作者 context 与 design 关联；纯文字纠错不增加计划版本。design 关联包含 path、version、目标 plan_version、任务至章节映射；指令要引用章节。脚本验证文件、章节标识、版本、作者来源并保存哈希，不能判断自然语言语义一致。失败或被引用章节哈希不一致时受影响新派发暂停（只算被引用章节，见[计划与设计关联](../../references/protocol-plan.md#计划结构)），恢复一致后重读状态。旧台账继续可读、不自动迁移；旧格式不可用于新建。局部任务增加批次时必须同时关联设计。运行中任务的负责人须保持该 attempt 登记的负责人，回收后才能交接；已有不一致旧记录先恢复计划与运行 attempt 的负责人一致，或完成后交接。

报告改动章节、影响、待决事项；用户已有授权可沿用，新增权限/预算不推定。字段和请求见[计划与设计关联](../../references/protocol-plan.md)。

init/plan_update 前核对[脚本强制的审阅预算配套](../../references/protocol-budget.md#审阅预算配套脚本门禁)：主执行、备用和负责人兜底的可用交付轮次须有独立审阅额度覆盖。联合审阅和共享预算按程序报告的保守需求预留；不能安排先返修再临时向用户申请复核额度。旧台账用 status/validate 查看 review_budget_coverage；报告缺口不授权自动提高预算。

纯文字纠错使用 `flow.py design_refresh --actor architect --revision <REV> --data -`，输入 version、reason、context，保留 plan_version。实质变化不能借此刷新放行。文档先写、计划登记失败时保留原台账，不手工改 JSON；按错误修正材料后重提，或恢复原有效文档。同步门禁覆盖关联任务和依赖它们的审阅/放行节点，未关联工作可继续；脚本无法判定语义影响时技术负责人负责精确范围。普通模型配置调整沿 set_models 的直接登记例外，不重新架构审批。
