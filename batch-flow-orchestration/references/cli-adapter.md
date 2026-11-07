# CLI 适配器（agent_cli.py）

`bf run` 通过 `scripts/agent_cli.py` 调用各角色的 CLI（AGY、Codex、Claude Code、OpenCode）。驱动器自动完成以下步骤；本文供排查与维护。

## profile

每个后端一个 profile，放在 `<任务>/.batch-flow/profiles/<backend>.json`。缺失时 `bf run` 自动执行一次：

```text
python3 <技能>/scripts/agent_cli.py probe --backend <agy|codex|claude|opencode> --profile <路径>
```

probe 不调用模型，只记录可执行文件路径、版本和 help。每次 run 复查版本；版本变化即停止，删除旧 profile 后重新 probe。probe 不证明认证、远端模型或语义兼容。

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
- `collect` 只读：检查退出码、JSON、非空最终回复、实际会话 ID、权限拒绝与 CLI 错误。不可用的调用由驱动器登记为无效（免扣）。退出码 0 不等于技术成功。
- 计划 `env.<backend>` 的 `require_host`、`add_dirs`、`approval_policy`（Codex：on-request/untrusted）自动转成启动参数。

## 受控入口的自动放行（Codex）

只在用户明确批准固定测试入口可在宿主运行时使用：在宿主实际加载的 `rules/*.rules` 中，只对入口绝对路径及有限参数前缀写 `prefix_rule(..., decision="allow")`，保留沙箱。命中时整个入口进程在沙箱外以普通用户运行。

- 规则按 argv 前缀匹配：入口必须单独、直接调用；组合命令或另套 shell/flow 会不命中，在沙箱内失败（这正是“外部阻塞”判定只认直接调用的原因）。
- 入口自身须拒绝额外参数、跨角色互斥、限时、失败恢复、留原始记录；未实现的目标非零退出。
- 新规则须在新启动的 CLI 中生效，并用 `codex execpolicy check` 与同版本 CLI 实测。
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
