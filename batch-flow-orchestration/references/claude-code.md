# Claude Code 宿主：交互与无交互使用方式

**Linux 无交互 CLI 固定使用[统一派发入口](linux-cli.md)**；本文 CLI 示例用于理解参数和维护核查，日常不重复编写调用代码。交互 Agent 子代理仍按第 2 节。

本文只写 Claude Code 的派发、续接、回收、计时和 stop hook 接法；职责、上下文隔离、预算与登记规则不变，仍按[协作指南](agent-collaboration.md)、[上下文规则](contexts.md)和[协议](protocol.md)执行。参数以本机 `claude --help` 为准，CLI 升级后重新核对；不使用 `--dangerously-skip-permissions`、`bypassPermissions` 或其他绕过权限的参数。

## 1. 两种方式与登记口径

| 方式 | 适用 | 角色调用 | 会话标识 |
|---|---|---|---|
| 交互模式 | 用户在 Claude Code 会话中运行，主会话担任编排者（或局部任务的技术负责人） | Agent 工具派发子代理 | Agent 返回的 agent ID |
| 无交互模式 | 由脚本、其他宿主或 `claude -p` 编排者批量推进，无人值守 | 每次角色调用一个 `claude -p` 进程 | 输出 JSON 的 `session_id` |

`plan.roles` 在任务开始时登记，任务中用户可用 `set_models` 调整。tool 建议写 `claude-agent`（交互子代理）或 `claude-cli`（无交互），两种方式不混用同一角色配置。model 写实际传入值：交互模式为 Agent 工具的 `model`（如 `haiku`、`sonnet`、`opus`、`fable`），无交互为 `--model` 的别名或完整模型名。effort：具体值显式传 --effort，default 则省略覆盖参数、继承 CLI 配置，不把 default 当字面值发送；本机 2.1.318 help 列出 low/medium/high/xhigh/max，模型是否支持仍以实际调用为准。交互子代理只在用户为该角色指定 effort 时传，否则登记 default。call.conversation_id 填上表的会话标识。

## 2. 交互模式（Agent 子代理）

**新建会话**：Agent 工具，`subagent_type` 用 `general-purpose` 或用户定义的角色 agent，`model` 取登记配置，`run_in_background: true`，prompt 为按[派发信封](prompts.md)展开的完整内容（含工作目录、读写范围、交付形态）。不要用 `subagent_type: "fork"`：它继承主会话完整上下文且忽略 model 覆盖，违反作者/审阅隔离和模型配置。新审阅节点首审、第三方、换备选、负责人兜底一律新建。

**续接**：同批执行返修、同节点合规 R2 用 SendMessage 发给登记的 agent ID，只补本轮差异；不能凭“最近的执行者”猜目标，不并发驱动同一子代理。

**只读审阅**：子代理默认继承会话权限，只读靠角色提示约束。需要硬隔离时，由用户在 `.claude/agents/` 定义只含 `Read, Grep, Glob, Bash` 的审阅 agent 类型，并在 roles 中登记（tool 写 `claude-agent:<类型名>`）；技能不自行创建或修改该目录。

**并行写入**：子代理共享主工作区。共享文件保持单一写入者，有冲突写入串行；确需隔离时可在授权内用 `isolation: "worktree"`，其改动位于独立 worktree，合并属于交付的一部分，由执行者交付说明、审阅覆盖。

**回执与登记**：Agent 调用返回的 agent ID 即工作 handle；将该次工具返回原文保存为不可变文件作为宿主回执，再 `register_dispatch`。子代理结束时宿主发送完成通知并唤醒主会话；不要在通知前轮询其输出、推测或代写结果。最终报告只交主会话，派发责任人原样保存后按[交付形态](prompts.md#8-结果与请求的最小形态)代交 finish。

**计时（10 分钟进度）**：主会话在两轮之间只能被通知唤醒，没有时间驱动的原生 schedule 时，用 Bash 工具 `run_in_background: true` 前台运行：

```text
python3 <技能目录>/scripts/progress_timer.py wait --ticket <绝对唯一临时路径> --task <任务ID> --goal "<具体目标>" --handle <agent ID>
```

600 秒后进程结束产生通知，即为脚本计时；timer_handle 填 Bash 后台任务 ID，timer_ticket 填 ticket 路径。到期汇报全部在途子代理并用新 ticket 重启；子代理完成时 `progress_timer.py cancel --ticket <路径>`。不用 nohup 或 `&`，否则丢失完成通知。宿主不提供子代理中间输出时，汇报写明派发时间与“尚无完成通知”，不猜测进展。

## 3. 无交互模式（`claude -p`）

**认证前提**：`claude -p` 进程必须能拿到认证环境变量（如 `CLAUDE_CODE_OAUTH_TOKEN`）。写在 `~/.bashrc` 的交互判断之后时，脚本、子进程和代理工具的非交互 shell 读不到，会出现 `subtype=success` 但 `is_error=true`、`result` 为登录提示。派发前用一次最小调用确认 `is_error=false`；不在提示、日志或台账证据中输出 token。

提示词经标准输入传入，参数用数组传递，不把提示正文拼进 shell。首次调用用 `--session-id <新UUID>` 预先指定会话，start 可直接登记真实 conversation_id，不必用 pending。

**已授权的写任务**（主/备执行、负责人兜底）：

```text
claude -p --session-id <UUID> --model <MODEL> --effort <EFFORT> --permission-mode acceptEdits --permission-prompts none --allowedTools "Bash(<本批验证命令的限定规则>)" --output-format json [--add-dir <已授权最小目录>]
```

`acceptEdits` 放行工作区内文件编辑，`--permission-prompts none` 使任何需要确认的操作自动拒绝而非挂起；构建/测试命令需在 `--allowedTools` 中精确列出，不用 `Bash(*)`。工作目录必须是本批实际开发根。

**只读审阅、第三方、只返回规划内容的架构调用**：

```text
claude -p --session-id <UUID> --model <MODEL> --effort <EFFORT> --permission-mode default --permission-prompts none --disallowedTools "Edit Write NotebookEdit" --allowedTools "Bash(<复跑验证命令的限定规则>)" --output-format json
```

**续接**：`claude -p --resume <session_id> ...`，每次显式带本次权限参数，不假定旧会话权限适合当前职责；不用 `-c/--continue`。固定适配器需同时给 --conversation <原生UUID> 和 --resume-from <上轮成功run-dir>，先核对原始记录，再在回收时核对同一 ID；不能使用逻辑名称或交互 Agent 的 agent ID。需要后续续接的会话不能加 `--no-session-persistence`；新会话不用 `--resume`/`--fork-session` 继承其他角色上下文。以下原生命令示例中的 --effort 仅用于具体值，default 时删除该选项和值。

**回收检查**（不能只看退出码或 `subtype`）：退出码；stdout 是否为可解析 JSON；`is_error` 必须为 false（已实测：未登录时 `subtype=success` 但 `is_error=true`、`result` 为登录提示）；`result` 是否有本轮有效内容；`permission_denials` 是否非空；`modelUsage` 中的实际模型是否与登记一致；`session_id` 登记为 conversation_id；`duration_ms` 与外层捕获的起止时间作为 timing 依据。权限拒绝、认证失败等启动问题按[启动失败与无效调用](agent-collaboration.md#启动失败与无效调用)分类处理。

```python
import json, subprocess
argv = ["claude", "-p", "--session-id", session_uuid, "--model", model,
        "--permission-mode", "acceptEdits",
        "--permission-prompts", "none", "--allowedTools", allowed,
        "--output-format", "json"]
if effort != "default":
    argv += ["--effort", effort]
proc = subprocess.run(argv, input=prompt, cwd=source_root,
                      capture_output=True, text=True)
stdout_path.write_text(proc.stdout); stderr_path.write_text(proc.stderr)
result = json.loads(proc.stdout)  # 再检查 is_error / result / permission_denials
```

**派发与计时**：交互主会话中用 Bash `run_in_background` 运行上述进程，后台任务 ID 为 handle，进程结束即完成通知，计时同第 2 节。由外部脚本驱动时，handle 用进程 PID 或作业标识，计时用 progress_timer.py 或外部调度。

**编排者本身运行在 `claude -p` 中**：进程在本轮结束时退出，退出后不存在可唤醒它的通知。因此无交互编排者只在 pre_yield 为 WAIT_USER 或 COMPLETE 时结束；有在途角色时同步分段等待其进程结束，不以 WAIT_HOST 结束进程。配置 stop hook 后可强制这一点。

## 4. Stop hook

两种方式共用 `scripts/stop_hook_pre_yield.py --host claude`。只在用户本人明确同意后写入 `.claude/settings.local.json`（不覆盖已有配置），示例：

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command",
  "command": "python3 <技能目录绝对路径>/scripts/stop_hook_pre_yield.py --host claude --store <本任务flow.json绝对路径> --role orchestrator --conversation <负责会话ID>"}]}]}}
```

负责会话 ID：交互模式在启动时用 `claude --session-id <UUID>` 指定；无交互用 `--session-id`。只绑定编排者（或局部技术负责人）会话；子代理结束触发的是 SubagentStop，不受此 hook 影响。hook 拦截一次后宿主带 `stop_hook_active=true` 再次请求结束、且台账 revision 与处置均未变化时，脚本放行并在 stderr 说明，避免无进展的反复拦截。注意 `--bare` 会跳过 hook，强制守护场景不用。配置后按[宿主接入](../workflows/host-setup/SKILL.md)验证拦截与放行，再声明已接入。
