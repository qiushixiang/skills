# AGY 与 Codex 宿主：子代理工具与 CLI 调用

**先区分主会话和角色进程**：Linux 上 AGY 担任编排者使用下节的受限启动器；AGY 原生角色用 define_subagent/invoke_subagent。另起 AGY、Codex、Claude CLI 角色进程时使用[统一派发入口](linux-cli.md)，不要求编排者逐轮手写 subprocess。Codex 原生 collaboration 工具仍按对应小节。 已明确授权固定设备测试入口的自动放行按 [Linux CLI 固定入口](linux-cli.md#固定设备测试入口的自动放行) 配置并实测，不为此全局关闭 Codex 沙箱。

本文只写 AGY 与 Codex 的调用形态、权限与回收检查；通用派发、回收、汇报与计时规则见[协作指南](agent-collaboration.md)，Claude Code 见 [Claude Code 宿主](claude-code.md)。参数以本机 help 为准，CLI 升级后重新核对；提示经参数数组或标准输入传递，不拼进 shell，不添加绕过权限的参数。

## AGY 编排者：Linux 受限启动

当前工程快捷入口（其他工程可直接用技能脚本并传 --workspace）：

Shell 入口实际位于 `<技能目录>/scripts/start-agy-orchestrator.sh`，项目根的同名文件仅为指向它的相对软链接。通过项目根软链接启动时默认使用链接所在目录作为源码工作目录；直接运行技能脚本时默认使用当前目录，也可显式 `--workspace` 覆盖。

```text
./start-agy-orchestrator.sh
./start-agy-orchestrator.sh --task-base <当前具体任务目录>
./start-agy-orchestrator.sh --task-base <任务目录> --model <主模型> --effort <effort>
python3 <技能目录>/scripts/start_agy_orchestrator.py --workspace <源码项目绝对路径> --task-base <任务目录>
```

`--task-base` 指定当前会话负责的具体任务目录，不是所有任务的父目录；不搜索、不列举、不自动挑选任务，也不约定任务放在哪里。相对路径以 `--workspace` 为基准，目录必须已存在。不传时，交互终端询问一次路径；非交互启动直接要求补充参数。选定路径显示在终端、初始提示和 SessionStart，并保存到会话 spec；切换任务应另行启动，不能在原会话里静默换台账。

`--workspace` 仍是源码项目及实际运行目录，`--task-base` 不改变它。默认正式台账为 `<任务base>/.batch-flow/flow.json`；`--store` 仅可覆盖为本任务内独立管理目录中的 flow.json，相对路径以任务 base 为基准，不能指向另一个任务。记录和运行回执随该台账存放。任务目录在源码项目外时，启动器显式传 --add-dir 挂载用户指定的任务目录。

`--dry-run` 只显示所选任务、能力、启动参数和自定义 agent 内容，不安装 hooks、不调用模型。接口在 AGY 1.4.12～1.5.1 上实测验证；启动器只拒绝低于 1.4.12 的版本，更新版本默认向下兼容、直接放行，并把实际版本写入 spec 的 `agy_version`；若新版本使门禁失效（例如 120 秒内未见 SessionStart 绑定即结束会话），按实际问题处理。1.5.0 本次验证主会话 hooks 与 CLI 版本检查，不据此声称所有原生子代理接口均已重新验收。用户已明确指定本启动器默认主模型为 `gemini-3.8-flash-medium`、effort 为 `medium`，不填即可使用；显式参数可覆盖，启动时显示实际值。此默认只用于编排者启动，不自动覆盖计划中的其他角色配置。

**账号切换命令（可选门禁放行）**：启动器 `--account-switch <绝对路径>`（可重复）登记用户自己的账号切换命令；启动时校验为可执行文件并写入 spec。编排者门禁、AGY run_command 门禁与角色审计只放行该绝对路径，参数仅限 `-k`、`-n` 和至多一个目标账号（`scripts/account_switch.py`），其他参数（如 `-K`）、管道或相对路径一律拒绝。何时切换由用户在会话中指示，技能不规定使用时机。

终端先显示能力清单，再用 `--agent batch_flow_orchestrator` 新建交互主会话。自定义主代理声明协调工具名单，但本机 1.4.12 的真实 init 回执仍返回包含编辑/MCP 的完整工具集，不能宣称工具已隐藏或裁剪。实际限制由独立的 PreToolUse 会话门禁执行：只允许原生协调工具、受限 view_file/write_to_file/run_command，其他工具（含编辑替换、源码搜索、MCP、权限扩张）均拒绝。SessionStart 将能力、固定脚本路径、台账及记录目录注入主会话，PreToolUse 在实际执行前拒绝不合法操作：

- view_file 只读技能 Markdown、绑定台账、管理 records 和启动器记录的子会话宿主元数据。
- write_to_file 只允许 `Overwrite=false`，在 `<台账父目录>/records` 新建 json/txt/md；不允许改源码、正式台账、启动器配置、agent 定义或 hooks。
- run_command 只允许绝对 Python 路径调用固定 dispatch_card.py next/show（绑定本次 store）及 flow.py/progress_timer.py/agent_cli.py，使用规范 shell 引用；不支持 shell 拼接、解释器任意代码、record/snapshot、参数缩写、复用终端或修改权限配置。flow.py 必须显式绑定本次 store。CLI 角色进程只能用启动器核查并保护的 profile；不传任意 settings，不扩大项目目录范围。
- 原生任务管理只查 list/status，不允许 send_input 注入别的终端；进度 schedule 只允许最多 600 秒的一次性计时。

**编排者终端不使用沙箱**：按用户选择，启动器不传 --sandbox，在本次 --settings 显式设 enableTerminalSandbox=false、toolPermission=request-review；不改全局配置，不设置 always-proceed 或全局 command(*)。自定义主 agent 不再强制 commandExecutionPolicy=sandbox。所有命令在宿主执行前仍经 PreToolUse 精确核对固定脚本、受保护 profile 和参数范围；任意 shell、直接运行 agy/codex/claude/opencode、业务构建及编辑仍拒绝。不传 BypassSandbox，也不把代理配置或认证目录加入主会话文件工具白名单。已有权限审批仍适用。

首次派发所需 CLI 前，先执行 `python3 <固定路径>/agent_cli.py check --profile <已核查profile>`（使用注入的绝对 Python 路径），保存原始输出，成功后再登记 start/decision_start。check 只运行 --version，不调用模型；不能据此声称认证、网络或代理任务已经验证。宿主启动前 probe 成功不证明另一个沙箱中可见；ENOENT 也可能来自未挂载或解释器不可见，不能直接报告用户未安装 CLI。各 CLI 使用自身认证、会话存储和网络；代理自身权限审批和 Codex 的 read-only/workspace-write 沙箱保持原配置。取消外层终端沙箱也会影响 AGY 原生子代理继承的终端执行环境，不能宣称子代理保留此前外层沙箱。主会话限制依赖宿主加载并执行 hooks，不构成 OS 层的身份隔离。官方配置依据：[终端沙箱](https://antigravity.google/docs/sandbox/?tab=cli)。

首次主会话 SessionStart 按启动器独有环境标识绑定 conversationId；其他启动器及子代理不受该主会话门禁影响。子会话 SessionStart/PreToolUse 的 conversationId/modelName 等实际元数据保存在启动记录的 children.jsonl，不能用模型自述替代。主会话执行前的允许/拒绝回执保存在 decisions.jsonl，保留参数哈希。门禁不通过全局 deny 规则实现，避免使执行者继承“禁止写代码/运行命令”；子代理仍遵守用户已有目录、沙箱和命令审批。

AGY 的 PreToolUse 对不属于本编排者的会话必须显式返回 `{"decision":"allow"}`，不能用空 `{}` 代表忽略；真实执行中空对象会使子代理工具被拒绝。该 allow 只是交还宿主原有权限判断，不是 auto_approve。其他会话的 Stop 显式返回 stop，避免被本编排者的续作门禁误伤。

**压缩上下文后的能力恢复**：不依赖首次 SessionStart 消息记忆 profile 路径。每次 CLI 的 status/pre_yield/apply 返回都会附当前绑定会话的 host_capabilities；派发动作的 cli_adapter.profile 给出对应受保护 profile。也可随时运行：

```text
python3 <技能目录>/scripts/flow.py capabilities --store <本任务flow.json>
```

使用启动器注入的绝对 Python 路径及规范引用。capabilities 不调用模型、不修改台账，即使尚未 init 也能查询；它仅凭继承的启动标识读取本任务绑定 runtime，不搜索其他会话或取最近目录，只返回公开路径和版本，不暴露配置正文或认证信息。未绑定、绑定冲突或损坏时明确报错；查询可用不替代实际 check/run 验收。使用猜测路径被门禁拒绝时，拒绝信息会附查询命令及当前已登记 profile。先恢复能力信息，不据此推断未安装/无权限、不要求用户换工具或模型。此功能对已有启动器会话的后续命令立即生效，无需重启。

启动器临时合并项目 `.agents/hooks.json`，不覆盖其他 hook；正常退出仅移除本次条目并恢复其创建的主 agent 文件，运行记录保留。若已有同名主 agent 内容不同，直接拒绝覆盖。同一工作区或同一台账只允许一个启动器，避免共享主 agent 文件在另一会话运行中被清理。异常强杀可能留下配置，先核对运行现场，不能手工覆盖现有用户配置。启动器不支持恢复旧主会话：当前 AGY 在 resume 时可能忽略 --agent；恢复任务通过新编排会话读取原台账完成。

Stop 同时连接 pre_yield 和[角色审计](role-audit.md)，明确用户停止仍优先。若真实 AGY 未回传 SessionStart，启动器退出时报接入未验证；配置文件存在和本地测试通过不证明真实宿主加载成功。该机制是宿主执行前门禁，不是 OS 隔离；同用户下其他进程或执行者仍可能访问配置，不能宣称防篡改或完全物理隔离。

新项目尚无正式台账时，Stop 只核查已采集的角色审计；审计正常即可报告缺少批准计划/角色配置并等待用户，不假报项目完成，也不因不存在 flow.json 无限续作。有正式台账后恢复完整 pre_yield 门禁。

**编排方式与 Claude Code 相同**：编排者[只运行 next](roles/orchestrator.md#派发只用-next两宿主相同)。本宿主两种调用形态，由角色 tool 决定：

- `agy-native`：AGY 原生子代理。next 先给出固定的 define_subagent（本启动器每类角色一次），再给 invoke_subagent（Role 为卡 ID，Prompt 指向派发卡）。角色 model 写 `inherit` 或与编排者相同时不传 Model（继承主会话模型）；写其他值时原样作为 Model 传入，实际模型仍以 children.jsonl 的宿主元数据核对。原生接口没有 effort 参数，继承主会话。启动器在 decisions.jsonl 记下每次调用的 Role/Name，next 据此判断是否已派发；子代理交付到达后，编排者按 next 的 todo 用 write_to_file 把最终报告原文写到卡目录 result.md，下一次 next 回收登记。原生子代理不续接旧上下文。
- agy/codex/claude-cli/opencode：`run_command` 运行卡中的 `agent_cli.py run`，next 以 `run/exit.json` 回收，进程仍在则等待（AGY 宿主内裸 claude 视为 claude-cli）。

Claude 子代理类配置（claude-agent）在 AGY 下报错，不静默替换；反过来 Claude 宿主遇到 agy-native 时按 AGY CLI 派发。台账锁与 Claude 启动器共用，中断后直接重新启动。

## AGY 原生：定义执行者，再实际派发

受限启动器下这两个调用由 next 生成（角色 tool 配 agy-native），编排者照抄即可；以下是接口核查记录与字段说明。


本启动器的自定义主代理在真实 1.4.12 运行时要求 PascalCase 定义字段（Name/Description/SystemPrompt），小写形式会明确报缺少这些必填项。[官方 hooks 工具表](https://antigravity.google/docs/hooks/#agent-collaboration)描述的 snake_case 属于不同接口，不能替代当前运行时校验。此处固定使用已验证启动方式和下列定义，不每轮重新猜参数。

首次调用工具 `define_subagent`，参数：

```json
{
  "Name": "batch_flow_executor",
  "Description": "Dedicated implementation executor for batch-flow tasks",
  "SystemPrompt": "你是 batch-flow 的执行者。严格在本次批次允许范围内修改代码并运行测试，交付结果与原始证据。只处理收到的任务，不派生代理，不修改正式台账、管理脚本或授权。",
  "EnableWriteTools": true,
  "EnableSubagentTools": false,
  "EnableMCPTools": false
}
```

EnableWriteTools=true 提供创建/编辑文件及运行命令的工具，不是预先批准所有写入和 shell 命令；仍须核对实际授权和执行环境。预定义类型仍须检查配置，不用 self 克隆受限编排者来充当执行者。审阅/第三方类型使用 EnableWriteTools=false；确需运行验证或需要文件只读硬隔离时走明确沙箱的 CLI 角色入口。技术负责人需局部派发时才按正式 dispatch_owner 配置相应子代理能力，不把执行者的 false 静默改成 true。

派发前读取 flow.py status，核对授权、版本、预算、call_config/context_hint；登记 start 后，调用工具 `invoke_subagent`。本启动器自定义主代理的真实调用已验证使用顶层单个 spec，不能套用通用 CLI 文档的 Subagents 数组：

```json
{
  "TypeName": "batch_flow_executor",
  "Role": "Batch Flow Executor <任务ID>",
  "Prompt": "<信封通用头 + subagent_payload.instruction + 本轮允许范围/验收/实际工作目录 + 交付形态>"
}
```

**模型不能靠示例推定**：Model 仅在当前工具 schema 提供时按用户指定或明确批准的映射传入；不能把本地二进制中的其他接口字段当作当前运行时 schema。flash/pro 属于层级标识，实际子会话模型仍须用宿主回执核对，不能仅凭请求参数声称选型已生效。effort 若没有原生参数，就核对实际继承配置是否符合计划。只有实际模型/effort 已确认符合本任务计划时才走原生路径（tool 可明确登记 agy-native）；模型来源或实际配置缺失就先补齐。计划要求精确配置而原生接口不能表达时，保持原配置，经 agent_cli.py 调独立 AGY/Codex/Claude CLI 角色进程，不擅自映射 flash/pro，也不换成主会话代做。

同模型默认继承时，上述不传 Model 的形式已在 Gemini 3.8 Flash Medium 主/子会话中核对实际 modelName。若本次工具明确提供 Model，且用户已指定 flash 或批准相应映射，可在单个 spec 中增加 `"Model":"flash"`；这不是对其他版本或当前默认映射的承诺，仍须核对真实子会话元数据。

2026-11-05 AGY 1.5.1 真实临时目录验收（next 驱动，执行者 tool=agy-native、model=inherit）：next 依次给出 define_subagent 与 invoke_subagent（Role=卡 ID），门禁逐条放行并记下标签，next 据 children.jsonl 找到实际子会话 ID 登记；首次运行中途重启后，新启动的 next 把未交付的 A0001 登记 INVALID，重派 A0002（retry_of A0001），子代理写出 NATIVE_OK，编排者用 write_to_file 写 result.md，next 回收为 VERIFIED；主子会话 modelName 均为 gemini-3.8-flash-medium。同次发现：**首次在会话内确认信任工作区时，hooks.json 在 SessionStart 之后才加载，该会话完全没有门禁**（无 owner/decisions，编排者还读了技能脚本）。启动器因此在 120 秒内未见 SessionStart 绑定即结束会话并提示重启；工作区已信任后再启动即正常绑定。门禁依赖的 hook 载荷、决策值和原生工具字段都是未文档化的运行时行为，升级后若出现门禁失效或误拒子代理，按实际问题核查并处理。

2026-11-04 的真实临时目录验收确认：主会话文件写入及任意 Python 命令被 PreToolUse 拒绝且无标记落盘；define_subagent/invoke_subagent 成功；执行者写入 EXECUTOR_OK 标记并运行打印命令返回 CHILD_COMMAND_OK；主子宿主记录均为 gemini-3.8-flash-medium。此结果只覆盖该版本、该模型及本次接入配置，不扩大后续任务授权。

取得实际子代理 ID 和原始回执，按协议登记会话/运行句柄、计时与 register_dispatch；原始工具记录和 children.jsonl 元数据用于核对实际配置，不自造回执。用 manage_subagents/manage_task 查询运行状态，合规续接用 send_message 指向原始 ID；不重新 invoke 相同写任务，不把状态查询算作新的技术轮次。结束后按原始交付代交 finish，技术载荷有误退原角色修正。主会话永不执行 payload 中的产品命令。

维护本启动器运行 `scripts/test_agy_orchestrator.py`，门禁审计变更另运行 `scripts/test_role_audit.py`、`scripts/test_stop_hook.py`；实际宿主接入仍需验证主会话被拒、子会话未误伤及真实模型匹配，不以单元测试替代。

## Codex 协作工具（spawn_agent）

宿主提供 collaboration 工具且本任务允许角色协作时：

- 首次派发用 spawn_agent；需独立上下文时 `fork_turns="none"`，不继承父会话。
- 空闲代理进入下一步用 followup_task；运行中补本轮材料用 send_message，不另起重复任务。补充消息不另算技术轮次。
- 返回的 agent ID 登记为 conversation_id；长任务用宿主等待/状态工具回收，超时或 running 不等于 INVALID/FAILED，也不自动重派。

## AGY 无头调用

`-p/--print` 没有交互审批入口，需要确认的工具可能被自动拒绝；部分版本仍返回 exit 0 / status=SUCCESS 而 response 为空，不能把 SUCCESS 或 num_turns 当有效交付。参考[官方无头说明](https://www.antigravity.google/docs/cli/headless/#permissions-in-headless-mode)和[模式说明](https://www.antigravity.google/docs/cli/modes/)。

**已授权写任务**（主/备执行、负责人兜底、确需编辑设计文件）在本批实际开发根运行，显式指定本次模式：

```text
agy --mode accept-edits --model <MODEL> --effort <EFFORT> --output-format json -p <PROMPT>
```

本节原生命令示例仅供维护核查：effort 为具体值时传 --effort，配置为 default 时省略该选项和值，继承 CLI 配置，不发送字符串 default。日常调用仍使用 agent_cli.py 的共用参数与续接检查。

accept-edits 处理工作区内文件创建/修改，不是仅限本批文件的沙箱；已选 --project 时核对其工作区映射；--add-dir 只加本批已授权且确需的目录。不能用 --mode plan 执行应落盘的代码任务。

**只读审阅、第三方、只返回规划内容的架构调用**把结论/计划/操作参数作为最终 response 返回（规划同时返回设计书 Markdown 原文），不写文件、不执行 flow.py 写操作；派发责任人原样保存并代交。不用 `--mode plan`：1.5.0 实测 plan 下模型只出计划、不执行会写文件的命令，审阅无法复跑构建/测试。改用 `accept-edits` 加本次 `--settings` 的 `permissions.deny: ["write_file(*)"]`（实测新建与编辑文件均被拒，终端命令照常运行；`agent_cli.py --access read` 已自动合并该规则）。这不是只读隔离，终端仍可写文件，只读靠角色约束；需改代码交执行者。

```text
agy --mode accept-edits --settings '{"permissions":{"deny":["write_file(*)"]}}' --model <MODEL> --effort <EFFORT> --output-format json -p <只读核查并直接返回结论的PROMPT>
```

```python
import subprocess
argv = ["agy", "--mode", "accept-edits", "--model", model,
        "--output-format", "json", "-p", prompt]
if effort != "default":
    argv += ["--effort", effort]
# 合规续接时追加 ["--conversation", conversation_id]，仍显式带本次mode。
proc = subprocess.run(argv, cwd=source_root, capture_output=True, text=True)
stdout_path.write_text(proc.stdout); stderr_path.write_text(proc.stderr)
# 保存returncode；再解析JSON及权限拒绝，不直接把returncode==0视为通过。
```

**文件许可与命令许可分别检查。** accept-edits 不放行 run_command；构建/测试、工作区外读取也可能被拒。先确认本批所需范围，用当前 CLI 支持的方式准备精确许可，例如 `write_file(<已授权绝对路径>)`、`command(regex:<本批验证命令的限定表达式>)`，不用 `write_file(*)`/`command(*)`，见[权限说明](https://www.agy.dev/docs/permissions?tab=cli)；不臆造 CLI 未支持的参数，不改全局 settings，不加 `--dangerously-skip-permissions`。

**回收检查**：退出码、JSON 可解析、status/error、response 有本轮有效内容、denied_actions（版本提供时）、stderr 中 auto-denied/permission denied/approval 通知，以及角色报告的实际产物/验证。空 response 且无拒绝信息时继续区分认证、超时或输出故障。失败分类见[协作指南](agent-collaboration.md#启动失败与无效调用)。

续接用 `--conversation <ID>`（从原始 JSON 取实际 UUID），不用 --continue/-c；新建与续接都显式选本次 mode。固定适配器还必填 --resume-from <上轮成功run-dir>，校验 backend、UUID、cwd 和原始成功记录；逻辑会话名及原生子代理 agent ID 不能用于 CLI 续接。

## Codex CLI

**写任务**：派发前核对 `-C` 工作区、获准修改目标的真实路径（含符号链接）与本机 `codex exec --help`。涉及 `.agents`、`.codex` 等受保护目录或主工作区外的已授权目录时，首次调用就显式 `--add-dir <已授权最小目录绝对路径>`，保留 `workspace-write`；项目 trusted 不代表这些目录可写。

```text
codex exec -C <WORKSPACE> -s workspace-write --add-dir <ABS_DIR> -m <MODEL> -c <EFFORT_CONFIG> --json -o <RESULT> -
```

`--add-dir` 是目录级可写范围，不是业务授权；提示仍列明准确文件，交付时核对实际改动范围。不改全局 trusted/settings，不用 `danger-full-access`、`--dangerously-bypass-approvals-and-sandbox`。续接前核对 resume 支持的参数并显式给出本次可写范围，确认原调用已结束；仍被拒时保留错误按工具受阻处理，不换工具、沙箱或扩大目录绕过。

**审阅**：

```text
首审：codex exec -C <WORKSPACE> -s read-only -m <MODEL> -c <EFFORT_CONFIG> --json -o <RESULT> -
复核：codex exec -C <WORKSPACE> -s read-only -m <MODEL> -c <EFFORT_CONFIG> resume <SESSION_ID> --json -o <RESULT> -
```

从 thread.started 事件取 thread_id，显式 resume 本节点 ID，不用 --last。需要 R2 的会话不能 --ephemeral；新第三方无需恢复时可用，但保留实际 ID 与结果。只读审阅保持 read-only，不为保存报告加写权限。
