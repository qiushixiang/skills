# 编排者角色审计门禁

本功能对应编排者[红线](roles/orchestrator.md#编排者红线)，是工具调用发生后的审计，不替代 OS 权限隔离，不自动回滚，也不能证明所有实际文件改动。仅修改本技能不会自动安装宿主 hooks；已存在的 stop hook 也需增加审计参数及调用采集后才生效。

## 采集与范围

使用 `scripts/role_audit.py` 从宿主标准 hook 输入采集，不依赖不稳定的完整 transcript 格式。AGY 读取 conversationId/toolCall.name/toolCall.args；Codex、Claude 读取 session_id/tool_name/tool_input。只采集显式绑定的编排者会话；其他代理会话不写入本日志，避免把执行者工作误认作编排者越权。日志只保存工具名、参数、执行目录和时间，不复制工具结果或用户提示。

在实际宿主接入配置中为编排者绑定以下命令（路径与 ID 均替换成当前任务真实值，父目录先创建）：

```text
python3 <技能目录>/scripts/role_audit.py begin --host <agy|codex|claude> --conversation <编排者ID> --log <管理目录>/role-audit.jsonl
python3 <技能目录>/scripts/role_audit.py record --host <agy|codex|claude> --conversation <编排者ID> --log <管理目录>/role-audit.jsonl
```

`record` 接所有 PostToolUse（包括 shell 和文件工具），须同步写入，不设异步 hook。Codex/Claude 的 `begin` 接 UserPromptSubmit，每轮用户输入启动一个新范围；AGY 没有同名生命周期事件，接 SessionStart，保守检查该会话本次启动后的全部调用，不擅自假定每次模型推理就是用户新轮次。其他宿主可由可信适配器在确切用户轮次开始时提供同形 begin 事件。

begin 追加新标记、保留旧记录；编排者不得为了通过审计自行重置范围或修改日志。新范围必须来自真实用户轮次或明确的新启动；重复 Stop 不能 begin，不能拿 PreInvocation 当 begin 而漏掉本轮早先的调用。

官方字段及事件：[AGY hooks](https://antigravity.google/docs/hooks/)、[Codex hooks](https://learn.chatgpt.com/docs/hooks)、[Claude hooks](https://code.claude.com/docs/en/hooks)。宿主不支持对应采集时，不能声称已启用越权检测。日志和 hook 配置仍依赖宿主防篡改；没有工具调用覆盖证明，`clean` 只说明已采集记录未发现规则问题。

## 检查与拦截

直接检查或在 stop hook 增加相同参数：

```text
python3 <技能目录>/scripts/flow.py --store <绝对flow.json> pre_yield --actor orchestrator --conversation <编排者ID> --audit-log <管理目录>/role-audit.jsonl
python3 <技能目录>/scripts/stop_hook_pre_yield.py --host <agy|codex|claude> --store <绝对flow.json> --role orchestrator --conversation <编排者ID> --audit-log <管理目录>/role-audit.jsonl
```

默认只有 flow.json 的父目录可作为管理文件写入范围；额外存放回执的目录须在上述检查命令显式追加 `--audit-management-root <已授权最小目录>`，不要授权整个产品根。直接运行 `role_audit.py check --log <日志> --conversation <ID> --management-root <管理目录>` 也可检查，不运行任何被审计命令。

已启用审计时，以下情况输出 `ROLE_BOUNDARY_BLOCKED`、can_yield=false：

- 调用 Git 写操作、直接运行常见构建/测试工具，或调用产品 build/test 脚本。
- 文件编辑目标在配置的管理目录之外，包括 patch 移动目标或经软链接越界；即使在管理目录内，直接用文件工具修改正式 flow.json 也属于越权。
- 日志缺失、损坏、没有当前绑定的 begin、未知工具/命令或不能可靠解释的动态 shell。后者标为 unavailable，不能冒充已证明越权，也不能视为审计通过。

将 `git commit` 写在 Agent 的派发 prompt 中不会被当作主会话执行；真实 shell 工具调用才按命令审计。固定管理脚本只允许核对的真实路径；不允许借 flow.py 的子命令执行接口或 snapshot 代做产品命令/差异捕获。解释器任意代码、复杂 shell、重定向等当前只报告 unavailable，不声称能完整解释所有行为。

明确的越权或审计缺失不会被原有“重复 Stop 且状态未变化”的防循环规则放行；编排者应保存现场、停止产品操作、交责任人诊断。未知事件需维护适配器/宿主接入，不由编排者改日志、放宽规则或自动回滚。用户明确停止、宿主中断及预算终止继续优先。诊断完成后仍保留原审计证据，后续真实用户轮次/新启动才建立新范围。

未传 audit-log 时 `boundary_audit.status=not_configured`，原有任务状态/计时门禁保持原义；这不是已通过审计。architect 的局部派发不套用编排者禁止实施规则。技能维护后运行 test_role_audit.py、test_stop_hook.py，以及 flow.py 的两套测试。

## 接入验收

实际安装/调整宿主 hook 前按用户授权边界执行；不要覆盖其他 hook。用非产品临时目录验证 begin 和 record 确实收到本会话事件，再验证其他会话被忽略、直接 Git 写/编辑调用被拦、正常代理派发不误报、缺失日志会阻断、用户停止能退出。没有实际完成这些接入验证，只能报告代码测试通过，不能报告现场已守护。
