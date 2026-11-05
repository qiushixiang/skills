---
name: batch-flow-recovery
description: 处理严重技术阻塞、环境失败、预算耗尽和裁决恢复。
---
# 阻塞诊断与恢复

普通缺陷直接按归属返修；技术方向、预算、范围、重要契约或严重阻断才升级技术负责人。保留原始报错、有效工作和实际计时；环境失败与技术投入分账，无有效工作可有限续作，真实技术失败不得伪装 INVALID。主执行→备选一次→负责人有限兜底，预算与历史不因重建或转分批重置。

技术负责人在对应技术链路上下文决定原因、影响和最小恢复条件；跨组契约由总体方案协调。实质变更走[计划同步](../plan-sync/SKILL.md)，新增授权或预算按用户原始来源处理。resolve 结构化恢复条件与版本证据，必要审阅/确认关闭后由责任人 close 并实际继续派发。workaround 给边界、验证和退出条件。不可改脚本校验或直接编辑台账绕过门禁。

WAIT_EVIDENCE 按[等待诊断与重检](../../references/protocol-decisions.md#wait_evidence-诊断与重检)机械登记并派负责人；不要根据等待报告自行猜测用户所有权。

按事件读取[技术预算与续作](../../references/protocol-budget.md#技术预算记账与续作)、[严重阻塞与临时授权](../../references/protocol-decisions.md#严重阻塞与临时workaround授权)、[用户离开状态](../../references/protocol-decisions.md#用户离开状态)。严重阻塞先诊断，已授权 workaround 先争取恢复本批，确实不可行才转不依赖它的工作；离开状态必须用户明确启用，不扩大预算或权限。非阻断建议按[工程规范](../../references/engineering.md#审阅边界与问题处置)合并处置，不走阻塞链。
