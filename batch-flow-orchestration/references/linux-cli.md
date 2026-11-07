# Linux CLI 固定派发入口

Linux 上使用 AGY、Codex CLI、Claude Code CLI、OpenCode CLI 时统一运行 `scripts/agent_cli.py`，不在每次派发时重写 subprocess、试参数或研究续接方法。原生 Agent/collaboration 工具仍按对应宿主说明，不套用 CLI 包装器。

本机 2026-11-04 核查版本：AGY 1.4.12/1.5.0、Codex CLI 0.164.0、Claude Code 2.1.318。常规自动化测试使用假 CLI；真实 Codex 新建/续接验收见本文末尾，不能外推为其他模型、其他 CLI 或产品任务已经通过验收。

2026-11-06 新增核查 OpenCode v2.2.5。本机模型目录中 `deepseek/deepseek-flash` 的显示名为 **DeepSeek V4.1 Flash 1017**，对应用户指定的 deepseek-4.1-flash-1017；角色配置写 `{"tool":"opencode","model":"deepseek/deepseek-flash","effort":"default"}`，也接受 tool=opencode-cli。适配器不设模型默认值、不将其他 DeepSeek 模型静默当作此模型。其他安装需以其模型目录核对完整 provider/model 标识。

## 1. 首次接入或版本变化

AGY 受限启动器已生成受保护 profiles 时，先读取 flow.py status 中的 host_capabilities / actions[].cli_adapter.profile，或用 `flow.py capabilities --store <本任务台账>` 重新查询；不因上下文压缩丢失路径而重新探测、猜目录、扩大权限或请求换模型。只有查询明确缺少所需后端或版本检查失败时才处理真实接入问题。

在本任务的管理记录目录创建 profile（父目录须已存在），不改全局配置；同一 CLI 复用一个 profile。命令中的 `<技能目录>` 换成当前技能绝对路径：

```text
python3 <技能目录>/scripts/agent_cli.py probe --backend agy --profile <管理目录>/agy-profile.json
python3 <技能目录>/scripts/agent_cli.py probe --backend codex --profile <管理目录>/codex-profile.json
python3 <技能目录>/scripts/agent_cli.py probe --backend claude --profile <管理目录>/claude-profile.json
python3 <技能目录>/scripts/agent_cli.py probe --backend opencode --profile <管理目录>/opencode-profile.json
```

只 probe 本次计划实际使用的 CLI。probe 不调用模型，保存可执行文件真实路径、版本、help 和核查时间。每次 run 只复查版本；版本变化就停止，重新 probe 到新文件。probe 只能核对参数表面兼容，不能证明认证、远端模型或升级后语义兼容；不兼容时按宿主接入问题处理，不由编排者临时改适配器。不要每轮重复 help、models、认证探索。

### 已启动会话新增 CLI

启动器只在启动时探测 profiles。新增后端后，旧会话不会因反复运行 next 自动登记它；可重启启动器，或由用户授权的会话外维护者执行：

```text
python3 <技能目录>/scripts/launcher_profiles.py --store <本任务绝对flow.json> --runtime <本会话明确绑定的启动器目录> --backend opencode
```

runtime 必须来自本会话的启动绑定，不按目录时间猜选。维护入口核对 store/launch/runtime 后，真实 probe/check，再以新文件登记缺失后端并原子更新本启动器 spec；已有 profile 不覆盖、同后端重复登记只复查版本。它不调用模型、不改正式台账或角色模型，也不启动任务。此脚本不在受限编排者工具白名单内；编排者不能借此自行修改受保护配置。维护完成后在原会话通过 capabilities/status 重新读取能力，再按 next 推进。

## 2. 派发与续接

### 用户口语指定模型：先查目录，再解析

模型变更先查询用户指定的后端，不要求用户提供完整 ID；只有用户要求跨后端比较时才查询全部已绑定后端。编排者通过 capabilities 取已核查 profile，使用统一管理入口，不直接调用原生 CLI 或读取认证配置：

```text
python3 <技能目录>/scripts/agent_cli.py models --profile <已绑定profile> --output <records内不存在的目录文件.json>
python3 <技能目录>/scripts/agent_cli.py resolve-model --profile <同一profile> --catalog <上述目录文件.json> --name '用户所说的模型名' --effort default
```

LLM 依据用户原话理解工具、模型和变体，使用目录中的显示名或完整 ID 调用 resolve-model；名称映射需向用户复述。脚本对大小写、分隔符和版本前缀 V 做确定性规范化；唯一结果返回 `status=resolved` 和可用于 set_models 的 config。多个候选返回 `needs_clarification`，LLM 用显示名/变体向用户确认；没有结果返回 `not_found`，不完整目录返回 `unverified`，均不自动选择近似模型。用户没有指定 effort 时继承 default，不重复询问。模型名自带 High/Medium/Low 变体与独立 effort 参数分别保留，不能互相推定。

例如用户说“AGY Gemini 3.8 Flash”，目录若只返回 High/Medium/Low 三个变体，则需要确认变体，不能静默选 Medium。目录中的完整 ID 不靠技能示例固化。LLM 可以从目录定位用户明确指定的版本或提供商，但不能把“指定版本”改为“最新版别名”。

目录记录 CLI 版本、查询来源、时间、profile 指纹及白名单模型元数据；一小时后或 profile 改变时重新查询，文件不可覆盖。AGY 使用 models，OpenCode 使用 api model.list --standalone，Codex 使用 app-server 的 initialize/model/list 元数据 RPC；这些入口不发送推理提示词。Claude 当前只有 help 中的模型别名，返回 complete=false，不冒报完整远端目录。各后端实际配置的默认模型没有可靠来源时记录 unknown（null），不从旧任务或模型排序推定。查询失败停止解析，不自动重试或更换模型。

目录只证明 CLI 广告的模型元数据，不证明认证、账号可用性或推理成功。查询/解析不改台账和角色配置、不派发代理；解析后仍按[用户指定模型直接登记](protocol-plan.md#用户指定模型的直接登记)执行 set_models。受限 AGY/Claude 编排者只接受启动器绑定 profile，目录读写限定 records；旧会话沿现有固定入口使用新命令，无需扩大原生 CLI 权限。

首次派发前，在实际将运行适配器的执行环境中运行 `python3 <技能目录>/scripts/agent_cli.py check --profile <本CLI的profile.json>`。它只核对固定二进制的 --version，不能证明认证/网络/模型可用；不可见时报告当前执行环境问题，不推断宿主未安装。AGY [受限启动器](hosts-agy-codex.md#agy-编排者linux-受限启动)按用户选择关闭编排者终端沙箱，check/run 仍受固定工具门禁限制，不传 BypassSandbox；其他宿主沿各自原有权限执行，不自行绕过沙箱。

登记 start/decision_start 前，再用同一 check 命令补上拟用的 `--model <模型> --effort <effort> --cwd <工作目录>` 做完整本地参数检查；续接同时提供下述 conversation/resume-from。它不调用模型，也不保证模型支持所有已通过语法检查的配置，真实可用性仍以远端完成记录为准。

使用派发卡时，`plan.cli_execution.<后端>` 的 require_host、add_dirs 和 Codex approval_policy 自动生成启动参数；父进程与目录检查在 start 前由脚本完成，run 再复查，guide 自动随卡传递。require_host 不清除外层限制，也不保证子进程已命中宿主放行规则；不满足时保留环境错误，不由编排者手工补参数。缺省保持旧行为，配置须来自本任务授权。

`effort=default` 的唯一含义是**不覆盖 CLI 自身配置和模型默认值**：四个后端均省略 effort 覆盖参数，不发送字符串 default，不映射成 medium。OpenCode 当前仅接受 default，未核验的 effort/variant 映射一律在启动前拒绝。request.json 同时保存请求值和 effort_override（继承时为 null），不把继承冒报为某个已测实际 effort。需要固定数值时按用户指定设置明确值；非法枚举和已知模型不支持的值在启动前拒绝。Codex 模型支持情况参见[官方推理说明](https://developers.openai.com/api/docs/guides/reasoning)。

先按 flow.py 的合法动作登记 start（裁决用 decision_start），再通过宿主可回收的异步/后台工具运行：

```text
python3 <技能目录>/scripts/agent_cli.py run --profile <本CLI的profile.json> --task <节点或裁决ID> --cwd <开发根绝对路径> --model <已批准模型> --effort <已批准effort> --access write --prompt-file <本轮完整指令文件> --run-dir <不存在的本轮记录目录>
```

只读审阅、只返回内容的技术规划/裁决使用 `--access read`；执行、修复和确需落盘的设计使用 `write`。从 status 的 `subagent_payload` 获取技术内容与调用配置，按[派发信封](prompts.md)准备提示；顶层 `orchestrator_todo` 只供编排者协调。包装器不替代角色指令、授权或验收。CLI 必须支持计划指定的 model/effort，不能静默替换；角色原文请求格式错误退原会话修正。

合规续接在同一命令追加 `--conversation <原始真实ID>`，每轮都使用全新的 run-dir。首审、第三方、换人及兜底不传 conversation。包装器不用最近会话，不自动重试。不得并发续接同一会话；重复派发前核查原宿主句柄。任务标识只用于关联，不认证身份。

四个后端续接均必填 `--resume-from <上一轮成功调用的run-dir>`：共用脚本核对其中原始 stdout/exit/request，要求 usable、backend、原生会话 ID 和 cwd 匹配后才启动，回收时再检查实际 ID 一致。AGY/Codex/Claude 使用 UUID，OpenCode 使用 `ses_…`，不互换、不跨后端续接。逻辑名称、仅有 ID 但无成功原始记录、失败或未结束调用均不能作为续接依据；不得改用最近会话或自动回退新会话。旧会话失效时，按上下文规则明确记录重建原因。历史 Codex 台账含逻辑名时 status 提示 fresh，不自动改写历史；已开始的 Codex 裁决登记修复见[上下文规则](contexts.md)。上述检查仅针对 CLI，不限制原生 Agent 工具的 agent ID 形状。

Codex 始终使用明确的 read-only/workspace-write 沙箱，并传 --skip-git-repo-check 以支持用户指定的多仓项目根或非 Git 任务目录；该选项仅省略 Git 目录前检，不修改全局 trust、不关闭沙箱。不要为了绕过前检把 cwd 静默换到另一个仓库。 用户已明确授权的固定设备测试入口可另按下节配置窄范围自动放行规则；不因此改成全局无沙箱模式。

额外授权目录可重复传 `--add-dir <最小目录绝对路径>`。AGY/Claude/OpenCode 可传 `--settings <本次已授权会话配置JSON>`，只加载为会话覆盖，不修改全局设置；不能以修复调用为名放宽权限。Codex 不接受此参数，采用明确沙箱参数。包装器不会自动授予构建命令权限；遇拒绝保留产物并按恢复流程处理，编排者不代做验证。

| CLI | read / write | 续接 | 回收 ID |
|---|---|---|---|
| AGY | plan / accept-edits | --conversation | conversation_id |
| Codex | read-only / workspace-write | exec 的目录和沙箱选项放在 resume 前，显式 ID | thread.started.thread_id |
| Claude Code | plan / acceptEdits，permission-prompts=none | --resume | session_id |
| OpenCode V2 | build agent；read 追加 edit deny，write 保留现有权限 | --session（不使用 --continue/--fork） | JSONL sessionID（ses_…） |

read 模式（AGY accept-edits 加 write_file 拒绝、Claude default 去掉编辑工具）不是文件只读隔离；本入口不实现 OS 角色隔离，也不认证模型实际用量。包装器拒绝已知绕过权限的配置值，但不是通用权限配置审计器。现有用户/项目设置仍可生效，需要硬隔离时按宿主权限与 OS 边界配置。

OpenCode 只接入 V2 参数和配置契约：`run --standalone --format json --agent build --model <provider/model>`，提示词作为单个字面参数传入；不复用全局后台服务、不加 `--auto`。通过子进程 `OPENCODE_CONFIG_CONTENT` 合并已授权会话设置，read 在 build agent 最后追加 `edit:* deny`，`--add-dir` 转为指定目录的 external_directory 规则；不覆盖原 shell 拒绝或全局配置。该 read 模式也不是 OS 只读隔离，shell 仍受原规则约束。继承的 provider 配置与凭据只传给子进程，不复制到 request.json；记录仅保存策略意图和设置文件路径。V1 的 permission/bash/task 不套用到 V2 的 permissions/shell/subagent。参数、权限与配置以[官方命令](https://opencode.ai/v2/docs/cli/commands/)、[V2 权限](https://opencode.ai/v2/docs/permissions/)和[配置](https://opencode.ai/v2/docs/config/)为依据。

### 固定设备测试入口的自动放行

仅在用户明确批准固定测试入口可在宿主自动运行时使用。主执行者、备选、负责人兜底和独立审阅者均沿任务指定模型，通过同一入口运行各自负责的必要验证；不以换角色规避权限或独立审阅。

在该宿主实际加载的用户或可信项目配置层的 `rules/` 内设置 `.rules`，只对固定绝对入口路径及有限操作前缀配置 `prefix_rule(..., decision="allow")`。保留 CLI 的 read-only/workspace-write 沙箱；命中规则时整个测试入口及其子进程在沙箱外以普通用户运行，不对任意 shell、解释器、`flow.py` 或 `sudo` 配置广泛放行。入口自身调用 `flow.py record` 保存验证原始结果。规则需在新 CLI 启动时加载，并通过 `codex execpolicy check` 和实际同版本 CLI 验证；read/write 角色、首派及续接均须使用相同有效配置。

`prefix_rule` 匹配 argv 前缀，不是完整参数等值校验，也不绑定脚本哈希。入口须拒绝额外参数、任意命令/路径/接口，固定下游映射；入口及其下游代码是否可修改、适用的信任范围与固定版本由用户授权和任务独立审阅约束。薄包装器加载任意可改工作区脚本不构成内容隔离。设备运行应有跨角色互斥、时限、失败恢复和不可覆盖原始记录；未实现批次必须非零退出，不能借用其他批次冒充通过。

调用使用单条明确的入口命令；复杂 shell 包装可能按整个 shell 命令匹配，不能认为脚本内部某条已放行命令会使整个脚本自动出沙箱。CLI 必须由具备合法宿主执行能力的父进程启动；已经受外层 NoNewPrivs、capability 或系统调用限制的子进程，不能靠内层规则清除限制。发生拒绝时按环境恢复流程保留原始记录，不关闭全局沙箱或扩大 sudo 授权。

官方规则与范围见 [Codex Rules](https://learn.chatgpt.com/docs/agent-configuration/rules)。项目的固定路径、操作参数和 sudo 端点放在项目自己的测试说明与规则文件，技能不写死工程路径。

## 3. 运行回执、计时与回收

包装器保持前台运行，由宿主提供异步工作句柄和完成通知；不用 nohup、`&` 或 CLI 自身的脱离后台模式。脚本输出 `CLI_STARTED` 并保存 `receipt.json`（真实子进程 PID，证明启动事实而非持续存活）。登记宿主句柄，并依[派发协作规则](agent-collaboration.md#实际派发与交还控制权)另行启动 600 秒计时和 register_dispatch；包装器不自动写台账或创建定时器。

run-dir 原样保存 request.json（实际参数、stdin）、stdout、stderr、receipt.json；正常回收后另存 exit.json。记录包含完整任务指令，只保存在本任务有权限的管理目录，不复制凭据。不要把退出码 0 当技术成功；包装器输出 usable 只表示传输检查通过。

```text
python3 <技能目录>/scripts/agent_cli.py collect --run-dir <本轮记录目录>
```

负责人已登记 decision_start 且会话仍为 pending 时，派发卡沿用该开始记录与原时间，不重复登记；配置必须一致且不能已有其他活跃派发卡。已有真实会话不走此分支。

续接证据在当前任务台账的 records 内按真实适配器 request.json 定位，兼容旧 records/<名称> 与新 records/dispatch/<卡>/run 两种布局；仍须 collect 可用且原生会话 ID 精确匹配，不搜索其他任务，也不凭 UUID 字符串放行。

collect 只读，不重派。检查 CLI 退出码、有效 JSON、非空原文、实际会话 ID、显式权限拒绝和 CLI 错误；续接 ID 必须一致。Codex 还要求 turn.completed，Claude 同时要求 subtype=success 和 is_error=false。原始输出中未提供的权限/模型信息不能据此推定合规。技术正确性仍由角色交付和独立审阅证明。

OpenCode 回收要求同一 ses_ 会话、最终 `step_finish.part.reason=stop` 和对应 messageID 的非空 text；中间说明不冒充最终交付，缺少结束事件、混合会话或 error/权限拒绝均标为 unusable。普通已处理的构建/测试失败不等于传输失败，技术结论仍按角色原交付登记。

没有 exit.json 返回 CLI_INCOMPLETE；可能还在运行，也可能 supervisor 已中断，须查宿主句柄和原始输出，不能认定任务仍活跃或直接重派。中断后不自动强杀可能正在写入的工作。run-dir 已存在就拒绝启动，防止覆盖证据。包装器 run 退出 0 表示 usable，2 表示检查失败；失败不自动等于 INVALID，须按[有效投入分类](protocol-budget.md#技术预算记账与续作)判断。

统一派发卡回收CLI时直接引用原始 `run/receipt.json`、request/stdout/stderr/exit记录；`receipt.txt` 仅用于原生子代理路线。CLI快速完成而未轮询登记回执时仍可正常finish，不补造原生回执、不因回收失败重派已完成工作。run 目录只有 exit.json 而缺 request.json 不是真实运行记录，next 按中断收口；不得手写 run/exit.json、receipt 等运行文件来“作废”派发卡，作废按 next 的中断登记处理。

传输可用后，派发责任人引用 stdout 原始交付，补真实会话与实际调用记录，按协议代交 finish/resolve，技术结论不得自行改写。最后取消本轮计时并重读 status/pre_yield。

维护适配器须运行 `scripts/test_agent_cli.py`；修改 flow.py 还须运行 `scripts/test_flow.py`、`scripts/test_review_regressions.py`。日常派发无需运行这些维护测试。

## 4. 真实 CLI 接入验收

用户授权相应模型调用后运行共用验收脚本：`python3 <技能目录>/scripts/test_agent_cli_live.py --backend <agy|codex|claude|opencode> --model <已批准模型> --effort <已批准值或default>`；旧 test_codex_cli_live.py 仅为同一脚本的兼容入口。此脚本在临时目录通过正式适配器执行两次真实调用并请求 read 模式：新建并记住随机标记，再用原生 ID 和上轮成功记录续接，第二轮不提供标记却须返回它，并核对两次会话 ID 一致。read 请求不是 OS 只读隔离。报告和原始记录留在临时目录，不接触任务台账或产品代码；失败保留证据并停止，不自动重试。仅 --version/help 或假 CLI 测试不称为真实模型及续接验收，单个后端成功也不替代其他后端验收。

2026-11-06：OpenCode v2.2.5 + deepseek/deepseek-flash（显示名 DeepSeek V4.1 Flash 1017）+ effort=default 已通过上述新建/续接两次真实调用验收，实际 ses_ ID 一致且续接保留前轮随机标记。此结论仅覆盖该模型的 CLI 传输与上下文恢复，不代表产品验收或 OS 权限隔离。

Claude 后端认证：在 Claude Code 宿主内，Bash 子进程拿不到 `CLAUDE_CODE_OAUTH_TOKEN`（宿主主动删除）。适配器依次使用继承的 token/API key、0600 token 文件（`$BATCH_FLOW_CLAUDE_TOKEN_FILE` 或 `~/.config/claude-code/oauth_token`，经文件描述符传入）、CLI 自身登录；`request.json` 只记来源与文件路径，`check` 输出 `auth.source`。
