---
name: batch-flow-host-setup
description: 检查宿主真实计时与 stop hook 能力并验证任务绑定。
---
# 宿主配置与接入验证

需要编排者越权检测时，按[角色审计门禁](../../references/role-audit.md)绑定 begin、同步 PostToolUse 采集及 Stop 的 audit-log；未采集调用记录不能声称已启用审计。当前功能是事后检测，不自动回滚，不替代工具白名单和 OS 隔离。

优先当前宿主实际暴露、可唤醒负责会话的一次性原生 schedule。AGY [官方 schedule](https://www.antigravity.google/docs/commands/schedule) 支持 DurationSeconds、Prompt、TimerCondition；使用一次 600 秒，到期汇报后按需重设，任务结束取消，headless 会等待 timer。2026-10-26 本机 Codex CLI 0.164.0 无定时创建入口；[官方 automations](https://learn.chatgpt.com/docs/automations?surface=app)未给 CLI 管理界面。Codex App/Web 仅实际暴露可用工具且能唤醒时使用；queue、sleep 和完成通知不是 schedule。无原生能力用 progress_timer.py；无宿主通知须主动短轮询，不能承诺离线保证。

Claude Code 两种使用方式的计时与 hook 配置见 [Claude Code 宿主](../../references/claude-code.md)。

stop hook 显式绑定 `python3 <技能目录>/scripts/stop_hook_pre_yield.py --host <agy、codex或claude> --store <本任务绝对flow.json> --role <orchestrator或architect> --conversation <负责会话真实ID>`。缺少会话绑定不会启用拦截；不扫描固定产品路径，不拦其他执行或审阅会话。局部负责人只检查承接范围；等待外部依赖时先交回全局编排，不能声称存在未登记的后台进程。

AGY [Stop 协议](https://www.antigravity.google/docs/hooks)继续用 `decision=continue`，Codex [Stop 协议](https://learn.chatgpt.com/docs/hooks?surface=cli)与 Claude Code Stop hook 继续用 `decision=block`。宿主在 hook 已拦截过一次后再次请求结束（`stop_hook_active=true`）且台账 revision 与处置均未变化时，脚本放行并在 stderr 说明，避免无进展的反复拦截；有新登记后恢复正常拦截。配置时用真实绑定的 READY 任务验证拦截，用其他会话、完成/等待及明确停止验证放行，再启用；不覆盖无关全局配置。用户停止、宿主终止、预算限制优先。自然语言暂停不能依赖某宿主未提供的 lastUserInput 字段；先解除本会话 hook 绑定或由宿主适配器明确传递停止状态。文件存在不代表已接入；不支持拦截时说明限制并主动检查。
