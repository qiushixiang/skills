# Claude Code 宿主：交互与无交互使用方式

**Linux 无交互 CLI 固定使用[统一派发入口](linux-cli.md)**；本文 CLI 示例用于理解参数和维护核查，日常不重复编写调用代码。交互 Agent 子代理仍按第 2 节。

本文只写 Claude Code 的派发、续接、回收、计时和 stop hook 接法；职责、上下文隔离、预算与登记规则不变，仍按[协作指南](agent-collaboration.md)、[上下文规则](contexts.md)和[协议](protocol.md)执行。参数以本机 `claude --help` 为准，CLI 升级后重新核对；不使用 `--dangerously-skip-permissions`、`bypassPermissions` 或其他绕过权限的参数。

## 0. 编排者受限启动与派发卡（Linux，推荐）

用户选定 Claude Code 主会话担任编排者（可用 haiku 等轻量模型）时，经启动器开会话，不手工拼 hook 与派发提示：

Shell 入口实际位于 `<技能目录>/scripts/start-claude-orchestrator.sh`，项目根的同名文件仅为指向它的相对软链接。通过项目根软链接启动时默认使用链接所在目录作为源码工作目录；直接运行技能脚本时默认使用当前目录，也可显式 `--workspace` 覆盖。未给 `--task-base` 时在终端询问，不自动选择任务。

```text
./start-claude-orchestrator.sh
./start-claude-orchestrator.sh --task-base <当前具体任务目录>
./start-claude-orchestrator.sh --task-base <任务目录> --model <主模型> [--effort <effort>] [--dry-run]
./start-claude-orchestrator.sh --store <flow.json> --resume-launch <launch_id>   # 续接同一编排会话
```

启动器只在 `<台账目录>/.claude-launch/<launch_id>/` 写 spec、settings、审计日志和 CLI profile，不写 `.claude/` 或全局配置；以 `--session-id` 预绑会话，经 `--settings` 挂 SessionStart（压缩/续接后重注入能力）、UserPromptSubmit/PostToolUse（[角色审计](role-audit.md)）、PreToolUse（`orchestrator_guard.py`，带 `--card-root`）、Stop（pre_yield）；经 `--agents` 定义 `bf-executor`/`bf-architect`（可编辑）和 `bf-reviewer`（无编辑工具），角色均无 Agent 工具；环境变量 `BATCH_FLOW_CLAUDE_LAUNCH` 使 `flow.py status` 给出 host_capabilities、CLI profile 和每个派发动作的 `dispatch_card_command`。仅 2.1.318 已验证，升级后先核对接口。

派发卡 `scripts/dispatch_card.py`：`make` 把一个 dispatch/delegate_to_lead 动作生成不可变的 `records/dispatch/<卡>/prompt.md`（通用角色头、设计书定位、角色入口、台账原文 instruction、交付结构、原始载荷）和逐字可抄的宿主调用：claude-agent/claude-code 角色为 Agent（短提示指向 prompt.md 及其 sha256，`resume` 时为 SendMessage），agy/codex/claude-cli 为 `agent_cli.py run` 后台命令；`start`/`register`/`finish` 按卡机械调用 flow.py（显式 `--revision`），finish 从交付首行“结果：<值>”取结果，缺失则退回角色，不代填；`show` 在压缩后恢复各卡进度。拦截脚本只放行与某张卡完全一致的 Agent、SendMessage 和 `agent_cli.py run`，编排者无法自拟派发内容。Agent 路由要求模型为别名、effort=default（Agent 工具不能传 effort）；非任务动作（如 CONFIRM_CONDITION）只回收交付，登记由派发责任人按交付原文代交。

## 1. 两种方式与登记口径

| 方式 | 适用 | 角色调用 | 会话标识 |
|---|---|---|---|
| 交互模式 | 用户在 Claude Code 会话中运行，主会话担任编排者（或局部任务的技术负责人） | Agent 工具派发子代理 | Agent 返回的 agent ID |
| 无交互模式 | 由脚本、其他宿主或 `claude -p` 编排者批量推进，无人值守 | 每次角色调用一个 `claude -p` 进程 | 输出 JSON 的 `session_id` |

`plan.roles` 在任务开始时登记，任务中用户可用 `set_models` 调整。tool 建议写 `claude-agent`（交互子代理）或 `claude-cli`（无交互），两种方式不混用同一角色配置。model 写实际传入值：交互模式为 Agent 工具的 `model`（如 `haiku`、`sonnet`、`opus`、`fable`），无交互为 `--model` 的别名或完整模型名。effort：具体值显式传 --effort，default 则省略覆盖参数、继承 CLI 配置，不把 default 当字面值发送；本机 2.1.318 help 列出 low/medium/high/xhigh/max，模型是否支持仍以实际调用为准。交互子代理只在用户为该角色指定 effort 时传，否则登记 default。call.conversation_id 填上表的会话标识。

## 2. 交互模式（Agent 子代理）

**新建会话**：Agent 工具，`subagent_type` 用 `general-purpose` 或用户定义的角色 agent，`model` 取登记配置，`run_in_background: true`，prompt 为按[派发信封](prompts.md)展开的完整内容（含工作目录、读写范围、交付形态）。不要用 `subagent_type: "fork"`：它继承主会话完整上下文且忽略 model 覆盖，违反作者/审阅隔离和模型配置。新审阅节点首审、第三方、换备选、负责人兜底一律新建。

**续接**：同批执行返修、同节点合规 R2 用 SendMessage 发给登记的 agent ID，只补本轮差异；不能凭“最近的执行者”猜目标，不并发驱动同一子代理。

**只读审阅**：子代理默认继承会话权限，只读靠角色提示约束。需要硬隔离时，由用户在 `.claude/agents/` 定义只含 `Read, Grep, Glob, Bash` 的审阅 agent 类型，并在 roles 中登记（tool 写 `claude-agent:<类型名>`）；技能不自行创建或修改该目录。也可在用户同意后于编排者启动时传 `--agents '<JSON>'` 定义本会话的角色类型（相当于 AGY define_subagent，不写目录），如执行者 `tools: [Read, Write, Edit, Bash]`、审阅者 `tools: [Read, Grep, Glob, Bash]`，并把类型名登记进 orchestrator_guard 的 `--agent-type`。2.1.318 实测：角色 tools 只能在会话可用工具内收窄，不能恢复会话级 `--disallowedTools` 已禁的工具；审阅者无 Write/Edit 但 Bash 仍可写文件，不是 OS 只读。

**编排者工具边界**：不要用启动参数 `--disallowedTools` 或 settings 的 deny 限制编排者，它们对整个会话生效，子代理同样继承（2.1.318 实测：主会话禁 Write 后子代理也没有 Write）。改用只绑定编排者主会话的 PreToolUse 前置拦截 `scripts/orchestrator_guard.py`：宿主对子代理内的调用会带 `agent_id`/`agent_type`，主会话调用没有，脚本据此只拦主会话，其他会话和子代理原样放行；`--resume` 续接保持同一 session_id，绑定继续有效。白名单：技能管理脚本（flow.py/agent_cli.py/progress_timer.py/workflow_metrics.py/dispatch_card.py，不含 record、snapshot 与 `--` 代跑）、读根内的 ls/pwd/stat/readlink 与 Read/Grep/Glob、管理目录内新建文件（不覆盖、不写 flow.json）、`--agent-type` 登记类型且显式传登记 `--agent-model` 的 Agent 派发（拒绝 fork、Explore 等），以及 SendMessage/TaskStop/ToolSearch/AskUserQuestion/定时类工具（给 `--card-root` 时 Agent、SendMessage、`agent_cli.py run` 须与派发卡一致）；其余（Edit、构建/测试/Git/sudo、管道与替换、未知工具与 MCP）一律拒绝。第 0 节启动器已自动配置；手工接入时仅在用户同意后写入编排者启动所用的 settings（不覆盖已有 hook），示例：

```json
{"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command",
  "command": "python3 <技能目录>/scripts/orchestrator_guard.py --host claude --conversation <编排者session-id> --management-root <任务base>/.batch-flow --read-root <任务base> --agent-model sonnet --agent-model opus"}]}]}}
```

这是前置拦截，与[角色审计](role-audit.md)的事后检测互补，不替代 OS 隔离。维护后运行 `scripts/test_orchestrator_guard.py`；接入时用临时目录验证管理脚本放行、产品命令/越界写/未登记子代理类型被拒、子代理写入不受影响。

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

**只读审阅、第三方、只返回规划内容的架构调用**（不用 `plan` 模式：2.1.318 实测 plan 下模型拒绝执行会写文件的命令，审阅无法复跑构建/测试；`agent_cli.py --access read` 已按此生成）：

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
