# CLI 适配器（agent_cli.py）

`bf run` 通过 `scripts/agent_cli.py` 调用各角色的 CLI（AGY、Codex、Claude Code、OpenCode）。驱动器自动完成以下步骤；本文供排查与维护。

## profile

每个后端一个 profile，放在 `<任务>/.batch-flow/profiles/<backend>.json`。缺失时 `bf run` 自动执行一次：

```text
python3 <技能>/scripts/agent_cli.py probe --backend <agy|codex|claude|opencode> --profile <路径>
```

probe 不调用模型，只记录可执行文件路径、版本和 help。每次 run 检查可执行文件可启动并记录当前版本；版本变化不阻止执行，不要求删除或重建 profile。request.json 分别保存本次实际版本 version 和探测时版本 profile_version；实际调用失败按既有异常恢复规则处理。probe 不证明认证、远端模型或语义兼容。

## 模型

计划里的 `roles.*.model` 必须是 CLI 实际接受的标识。用户口语指定模型时先查目录再解析，不猜近似模型：

```text
python3 <技能>/scripts/agent_cli.py models --profile <profile> --output <新文件.json>
python3 <技能>/scripts/agent_cli.py resolve-model --profile <profile> --catalog <上述文件> --name '<用户说的名字>' --effort default
```

运行中换角色模型用 `bf/cli.py --base <任务>/.batch-flow set-role <角色> --backend <后端> --name '<用户说法>' --approval <用户原话文件>`：自动完成查目录与解析，只有唯一结果才写入计划，候选多个或未找到时不改动并返回候选；目录存 `<base>/catalogs/`。换工具或模型后的下一次派发开新会话。

多个候选时向用户确认变体。`effort=default` 表示不覆盖 CLI 自身配置。已核验：OpenCode v2.2.5 的 `deepseek/deepseek-flash` 即 DeepSeek V4.1 Flash 1017（2026-11-06，新建与续接两次真实调用通过）。

## 调用、续接与回收

- 每次调用一个全新的 run 目录：`request.json`、`stdout`、`stderr`、`receipt.json`、`exit.json`；已存在的目录拒绝覆盖。
- 读写模式：审阅用 read，其他用 write。read 不是 OS 级只读隔离。

| CLI | read / write | 续接参数 | 会话 ID |
|---|---|---|---|
| AGY | plan / accept-edits | --conversation | conversation_id |
| Codex | read-only / workspace-write 沙箱 | exec resume（目录、沙箱选项在 resume 前） | thread_id（UUID） |
| Claude Code | plan / acceptEdits | --resume | session_id |
| OpenCode V2 | build agent；read 追加 edit deny | --session | sessionID（ses_…） |

- 续接必须提供上一轮成功调用的 run 目录（`--resume-from`）：核对 usable、后端、会话 ID 与 cwd 一致才启动。不用“最近会话”，不跨后端续接。
- `collect` 只读：检查退出码、JSON、非空最终回复、实际会话 ID、权限拒绝与 CLI 错误。不可用的调用由驱动器登记为无效；预算只依启动证明或负责人结算，不按不可用状态自动免扣。退出码 0 不等于技术成功。
- 计划 `env.<backend>` 的 `require_host`、`add_dirs`、`approval_policy`（Codex：on-request/untrusted）自动转成启动参数。
- Codex 的 write + on-request 使用经 profile.help 核验的 `--approve-for-me`，保留 workspace-write，不使用全权限。只有 `-c approval_policy=...` 不足以证明 exec/resume 已启用审批。read 角色不使用该快捷开关，避免它扩大到 workspace-write；若 CLI 不能兑现其声明策略，按不可用暂停，不能静默降为 never 或扩大读写权限。
- 本机 0.167.0 拒绝同时使用 `--sandbox` 与 `--approve-for-me`；自动审批分支用显式 `-c sandbox_mode="workspace-write"` 固定同一边界，并核对本次原生回执实际仍为 workspace-write。其他分支继续使用 `-s read-only/workspace-write`，不改用户配置文件。
- 声明了审批策略的 Codex 调用会监测原生会话中本次启动后的 turn_context，核对实际 approval_policy、sandbox_policy 和自动 reviewer。实际值写 execution-actual.json；错误策略、旧续接回执、缺失回执均使 collect 不可用。适配器在检测到不符时终止该调用树；调用仍保留在驱动器管理的进程组中，stop --now 照常覆盖。该检查不是隔离边界，不能用它代替原生沙箱或命令规则。

## 受控入口的自动放行（Codex）

只在用户明确批准固定测试入口可在宿主运行时使用：在宿主实际加载的 `rules/*.rules` 中，只对入口绝对路径及有限参数前缀写 `prefix_rule(..., decision="allow")`，保留沙箱。命中时整个入口进程在沙箱外以普通用户运行。

- 规则按 argv 前缀匹配：入口必须单独、直接调用；组合命令或另套 shell/flow 会不命中，在沙箱内失败（这正是“外部阻塞”判定只认直接调用的原因）。
- 受控入口自行留证，不能套用通用 `bf record -- <命令>` 模板。bf record 在启动子进程前拒绝再次包装当前计划的受控入口；交付门禁发现已执行的包装调用时，要求同轮纠正调用，不把它当作宿主权限故障。查看入口源码不属于包装调用。
- 入口自身须拒绝额外参数、跨角色互斥、限时、失败恢复、留原始记录；未实现的目标非零退出。
- 新规则须在新启动的 CLI 中生效，并用 `codex execpolicy check` 与同版本 CLI 实测。
- sudo 端点文件在沙箱内可见、可读甚至标为可执行，不等于能提权执行。沙箱的 NoNewPrivs/setuid/UID 映射可能使 sudo 在执行端点前就失败；不得据此改宿主 sudoers、属主或 setuid 位。排查时先对比沙箱与宿主的只读 stat 和 `sudo -n -l`，确认精确端点授权；实际入口依既有规则整段在宿主执行，不单独放行 sudo 或任意解释器。查询授权成功不等于已验证设备 down/up，本次是否执行过设备操作必须如实区分。
- 参考 [Codex Rules](https://learn.chatgpt.com/docs/agent-configuration/rules)；项目的固定路径与 sudo 端点写在项目自己的测试说明里。

## Claude 后端认证

在 Claude Code 宿主内启动的子进程拿不到 `CLAUDE_CODE_OAUTH_TOKEN`。适配器依次使用：继承的 token/API key、0600 token 文件（`$BATCH_FLOW_CLAUDE_TOKEN_FILE` 或 `~/.config/claude-code/oauth_token`，经文件描述符传入）、CLI 自身登录。`request.json` 只记录来源，不记录凭据。

## 真实接入验收

用户授权模型调用后：

```text
python3 <技能>/scripts/test_agent_cli_live.py --backend <后端> --model <模型> --effort <值或default>
```

在临时目录做新建与续接两次真实调用，核对续接保留上一轮记忆、会话 ID 一致。单个后端或单个模型通过不外推到其他组合。

## 维护

修改适配器后运行 `scripts/test_agent_cli.py`、`scripts/test_model_catalog.py`。

## 工单正文传输

Codex/Claude 用标准输入；OpenCode 用已探测的 `run --file` 附带完整工单；agy 用短指令读取固定工单文件。正文不得作为单个 argv，也不截断。request.json 保存输入文件与SHA；启动前检查及CLI创建失败写入 exit.json 的 launch_error，含阶段和未启动事实；外层创建失败由驱动器记录并回收。未创建进程不声称 CLI_STARTED，续作启动失败不退还已收到的实际工作轮次。续作引用保存的原始工单。离线假CLI须实际读取长文件核对保真，真实后端启动还需实际验证。


## 启动事实与预算

适配器只报告可机械确认的 `launch_error.started=false`。只有整次未启动（无已接收片段）可自动免扣；无结果、认证错误、回执缺失等其余事实转技术负责人结算，不能从输出文字猜测。续作启动失败也不能抹去此前的执行。恢复和选人原则见 [技术负责人](technical-lead.md#预算结算)。

驱动器根据调用生命周期及 collect.usable 判断异常，不解析错误文案：进程仍运行且无退出回执时继续等待；收到 exit.json 后的不可用结果属于已结束异常；监督进程已消失但缺退出回执时标记状态不明，不自动重派。新版实施/返修确认未启动或已结束异常后，同轮重试一次，连续两次异常再沿原计划阶梯切换执行者；各次原始目录和失败回执保留。此恢复策略与技术结算分离，不要求执行者在服务故障时产出移交协议，也不增加技术额度。
