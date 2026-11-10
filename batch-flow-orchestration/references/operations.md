# 运行与恢复

## 启动

计划已配置角色的后端声明 `env.<backend>.require_host=true` 时，驱动器在前台入口、后台创建和 ensure-run 恢复入口检查自身 NoNewPrivs。受限父进程（非 0）或无法读取状态时直接返回 `DRIVER_ENVIRONMENT_BLOCKED`，不 probe profile、不创建后台驱动器、不派发角色、不登记无效角色轮次。通过平台批准机制在既有授权宿主重新启动；不自动提权，不修改子 CLI 沙箱或计划中的宿主要求。未声明 require_host 的任务不增加该限制。现有角色启动前检仍保留。

聊天前端支持 **Claude Code、Codex CLI、agy、OpenCode V2**。用 `--backend` 选择，默认 Claude；前端选择不改变计划中执行者、审阅者等角色的工具和模型。

项目根目录的 `start-batch-flow.sh` 软链接指向技能的同名脚本，可从任意工作目录调用：

```sh
./start-batch-flow.sh <任务目录> [--model <前端模型>]
./start-batch-flow.sh <任务目录> --backend codex [--model <前端模型>] [--effort high]
./start-batch-flow.sh <任务目录> --backend agy [--model <前端模型>] [--effort high]
./start-batch-flow.sh <任务目录> --backend opencode [--model <provider/model>]
./start-batch-flow.sh --base <任务目录>/.batch-flow
```

任务目录必须明确指定；不带参数只显示帮助，不选择或恢复任务。`--binary` 指定所选后端的可执行文件或兼容包装器，不用于选择后端。OpenCode 前端省略 `--model` 时默认传入 `deepseek/deepseek-flash`，显式指定时以参数为准；其他前端省略 model/effort 时沿用该 CLI 的配置，不从计划角色推定。启动前端后先展示状态，等待用户指示，不自动启动任务驱动器。加 `--dry-run` 只生成启动配置并打印实际 argv，不调用模型。

agy 前端支持简称替换，显式映射维护在 [model-aliases.json](model-aliases.json)：`argon` 或 `Gemini 4 Argon` → `gemini-4-argon`，不自动指定 High/Medium/Low。其他简称如 `opus` 查询 `agy models` 并要求唯一匹配；`flash` 若有多个变体则报候选，不能代选。完整 ID 原样传入，未列入当前目录不等于账号一定可用。dry-run 只解析显式映射与完整 ID，不启动 CLI 查询目录。

启动时显示“输入名称 → 实际传入 ID”；原生 hook 的 `modelName`（Codex 为 `model`）保存到 `front/<backend>/ready.json`，收到的宿主报告值写入 `notifications.log`。参数解析结果和宿主回报分别留存，不用请求值冒充实际模型。映射只影响前端，不修改 plan.roles。Gemini 4 Argon 的基础 ID 依据[官方标识说明](https://antigravity.google/docs/enterprise/)，模型及账号可用性仍需由宿主确认。

| 前端 | 交互与权限约束 | 事件与 10 分钟汇报 |
|---|---|---|
| Claude | 原生交互；会话 `--settings` 注入 PreToolUse | 原生 Monitor 与 CronCreate，可唤醒模型 |
| Codex | 原生交互；`-c` 注入 developer instructions 与 PreToolUse；保留 workspace-write 和 on-request 审批 | 写入 `front/codex/notifications.log` |
| agy | `--prompt-interactive`；在 `front/agy` 的独立工作区加载 AGENTS.md 与 `.agents/hooks.json`，用 `--add-dir` 关联任务 | 写入 `front/agy/notifications.log` |
| OpenCode V2 | 原生 `mini --standalone`；会话环境叠加专用 primary agent 与 V2 工具/终端插件，保留用户已有配置与权限 | 插件通过会话 API 主动请求汇报，并写入 `front/opencode/notifications.log` |

四种前端共用同一命令白名单：只操作绑定任务、读文件、在 answers/ 新建原话文件，不改源码或手写工单状态。Codex 的 apply_patch 仅允许新建 answers/ 文件；agy/OpenCode 的原生工具事件转换后执行同一检查。配置只写入本任务的 front/，不改用户或项目既有的宿主配置。

OpenCode 使用 V2 的交互式 mini 界面，不是 print/run 单轮调用；`--standalone` 保证临时配置由本次私有服务加载，不连接已有后台服务。显式模型须为 `provider/model`；不支持单独的 `--effort`，省略时沿用模型配置。专用 agent 和插件在环境中叠加，不将继承的 provider 凭据写入文件或 dry-run 输出。终端命令继承环境前会移除本前端的 agent/插件，避免限制扩散到 bf 驱动器与执行角色。现有同名 `batch_flow_front` agent 冲突时报错，不覆盖。

Codex 首次启动或 hook 变更后需在 `/hooks` 中审阅并信任；启动器不绕过 hook 信任，也不绕过 CLI 权限或沙箱。agy 需要信任该前端工作区。Codex/agy/OpenCode 若 120 秒内没有收到真实工具门禁回执，启动器结束前端，不宣称门禁已生效；重新启动会清除旧就绪标记。同一任务、同一后端的前端有排他锁。

Codex/agy 前端的事件、定时报告与宿主模型回执只写日志，不向运行中的 TUI 共用 stdout/stderr 插入文字，避免破坏光标与状态栏重绘。需要实时查看时在另一终端运行 `tail -f <任务>/.batch-flow/front/<backend>/notifications.log`。这两个后端的定时报告不调用模型，也不会自动唤醒闲置模型；模型在下一次用户交互读取最新状态并处理运维。关闭前端会停止通知线程，已在后台运行的驱动器继续推进。

OpenCode 的现有服务端插件同时负责主动汇报：每 5 秒只读查询绑定任务的 report/watch，任务推进或等待期间每 10 分钟、以及关键事件后，用 `session.prompt(delivery: queue)` 请求前端读取最新报告并向用户汇报，会产生前端模型调用。只绑定本次专用 agent 的首个根会话；子会话、其他目录和 agent 不绑定，切换到其他 agent 后暂停投递。模型忙时只保留一个提醒；投递间隔至少 30 秒，失败最多尝试 3 次并复用消息 ID，后续状态更新可重新尝试。停止或任务完成后不再周期唤醒，仍监测恢复及新事件；退出时清理定时器和查询子进程。日志保留投递与故障记录，通知不向 TUI 的 stdout/stderr 写文字，也不作为用户批准。插件仅通过启动器的进程环境启用，正常启动 OpenCode 不加载，不修改用户或项目配置。

启动器同步本次 `PWD` 与任务目录，避免 OpenCode 使用继承的旧工作目录。前端进程移除 `DEBUG`：OpenTUI 会因任意非空 DEBUG（包括 `release`）向终端输出 `[Reconciler]` 调试日志，干扰显示。原值只在环境中暂存，并在派生终端中恢复，同时移除暂存变量；不改用户环境或配置。

宿主契约依据：[Codex hooks](https://learn.chatgpt.com/docs/hooks)、[agy hooks](https://antigravity.google/docs/hooks/)、[agy CLI settings](https://antigravity.google/docs/settings?tab=cli)。本地元数据核对版本为 Codex 0.167.0、agy 1.5.2；离线测试使用替身前端，不代表真实模型交互已验收。

OpenCode 依据 [V2 CLI](https://dev.opencode.ai/v2/docs/cli/commands/)、[V2 插件](https://dev.opencode.ai/v2/docs/build/plugins/server)。本机 v2.2.5 已验证专用 agent 与原生插件加载，并在临时任务中用默认前端模型实际运行 mini：临时缩短汇报周期后成功自动投递两次，输入草稿保留，窗口缩放后输入区与底栏正常，终端无 `[Reconciler]` 调试输出。正式周期仍为 10 分钟；修改后须重新启动前端以加载新插件。

也可直接调用 Python 入口：

```text
python3 <技能>/scripts/bf/front.py launch --base <任务>/.batch-flow [--backend claude|codex|agy|opencode] [--model <前端模型>]
```

前端会话通过 `bf run --background` 启动独立驱动器并立即返回；每 10 分钟按上表汇报状态。

也可以不用前端，直接运行：

```text
python3 <技能>/scripts/bf/cli.py --base <任务>/.batch-flow run [--background] --profile codex=<profile> --profile claude=<profile> [--parallel N]
```

## 回答问题

`attention.md` 是当前开立问题的派生报告，每次状态事务同步刷新，包括停止期间的答复和计划修订；不依赖驱动器运行。前端以 `report.ask_user` 为当前待决依据，不把旧通知或历史对话重复解释为待批准事项。

ask_user 只用于[六类新增用户决定](user-decisions.md)。report.ops 中的技术阻塞、协调次数用尽和工具故障属于“受阻待处理”，不归入“待你决定”，不因驱动器退出而自动改为用户问题。先处理已有授权内的运维和技术处置。

协调单的运维 retry/escalate 在记录具体处理说明后开放新的有界协调尝试；技术预算保持累计。历史 no_progress 暂停的显式 retry 仍交负责人诊断；新的 coordination_limit 只说明次数用尽，恢复需记录具体运维说明。不得无实际处置循环调用这些入口；相关权限或预算已变化时仍应核对真实恢复条件。

把用户原话写入 `<任务>/.batch-flow/answers/<n>.txt`，然后：

```text
python3 <技能>/scripts/bf/cli.py --base <任务>/.batch-flow answer WO-0012 --file answers/<n>.txt --approve|--reject
```

只有依赖该问题的工单会等待，其余工作照常推进。

拒绝或改变处置方向的答复须交给技术负责人，不因原对象已完成或作废而仅保存文本。脚本新开裁决并阻塞仍在等待原对象的工单，保留原单历史；不从自然语言直接开实施或扩大授权。有效契约内的已回答用户约束沿依赖链进入后续卡片，即使中间单已作废；不会因此传递作者会话或其他审阅者结论。

## 停止、恢复、重启

- `stop`：不再派发新工单，已在运行的调用结束后照常回收。`resume` 恢复。
- 驱动器没有可派发或在途工作时退出，包括等待用户时。前端在 `answer / resume / retry / escalate / unhold / grant / envelope / set-role / supersede` 成功后统一运行 `ensure-run`：复用上次 `run` 保存的配置，在需要时后台启动；不会解除用户 stop，单实例锁防止重复派发。首次运行仍用 `run`。直接使用 CLI 的用户也应在这些操作后运行 `ensure-run`。
- 驱动器随时可以重启：状态全部在文件里；上次未回收的调用会被接管或登记为无效；新版技术调用若不能证明整次未启动，转负责人裁决与结算。
- CLI 工具失败登记为无效。新版整次未启动有机械证明才免扣并有界重试；其他技术调用立即移交负责人，不按无回复推断免扣。
- profile 缺失时 `run` 自动 probe（不调用模型），见 [CLI 适配器](cli-adapter.md)。

## 调整

用户随时可以调整，每条都带 `--approval <base>/answers/<n>.txt`（用户原话，一次调整 = 一次修订 + 一条事件）。命令均为 `bf/cli.py --base <任务>/.batch-flow ...`：

- `models <后端>`：只读，查询模型目录；`set-role <角色> --backend B --name '<说法>' [--effort E]`：换角色模型，见 [CLI 适配器](cli-adapter.md)。
- `envelope --add|--remove <批次>...`：自动推进范围；`grant <交付目标> --implement|--backup|--lead|--repair|--review N` 或 `grant <审阅组> --review N`：只增不减。
- `hold <交付目标或批次>...` / `unhold ...`：只暂停这一部分；`supersede WO-n --reason R`：作废开立中的工单。
- 新增或改写交付目标属于计划修订，由技术负责人起草（`revise`），聊天前端不运行 `init`/`revise`。

## 运维（聊天前端）

新实施/返修交付后，旧审阅的一次性 recovery 指令失效并保留在历史中，下一次审阅续用原审阅会话并核对当前交付，不应用日志大小阈值。新审阅周期回到主执行者，不继承上周期的负责人兜底选择；已用额度仍累计。

负责人技术裁决与纯预算结算按同一交付目标共用会话，不应用输出日志大小阈值；不同目标分别保持会话。同目标裁决/结算串行派发，结算仍只做记账，不复审质量。会话缺失、工具/模型变化、作者独立性冲突或明确要求 fresh_session 时仍按实际原因处理。

历史任务若已被“仅修旧报告格式”的指令误送到旧快照，且该旧报告重新打开了已交付返修，可用受控入口恢复：

```text
bf/cli.py --base <任务>/.batch-flow restore-review WO-n --run WO-n-rN --approval <用户原话文件>
```

该操作只恢复最新已交付、已结算的返修并送原独立审阅；检查原恢复指令早于返修、审阅明确绑定另一已知旧版本、无在途或未决依赖。不改历史调用、预算、阻断或审阅结论，不将交付标为质量通过。已审当前交付的真实否决不可用此入口撤销。恢复后用 ensure-run 继续；用户 stop 保持有效。

- Codex 启动参数与实际权限策略不一致、或缺少本次原生会话回执，属于 CLI 不可用；新版按实际启动事实决定机械免扣还是交负责人结算，历史协议按 invalid_retry 有界重试后暂停；不能派角色反复核查权限来消耗实施额度。详见 [CLI 适配器](cli-adapter.md)。宿主父进程 NoNewPrivs=0 不代表每个工具命令都已出沙箱；受控入口须直接命中既有规则，不外套 bf record。
- `report --text`：给人看的中文进展（正在做、下一步、待你决定、角色、批次），不含工单号；`report` 输出完整 JSON（含 roles、next、batches、ops）。
- 计划来源的实施卡采用当前计划指令；裁决新开的实施卡保留专用指令，并附当前计划作为上下文。验证副作用和版本差异作为事实交角色判断；前端不维护路径豁免或替代审阅。
- 所有入口创建的审阅均等待审阅组覆盖目标的当前实施/返修；校验、开单和派发使用相同依赖。需要实施前设计审阅时，由负责人在计划中建立独立设计交付与设计审阅组。
- 新版实施/返修确认 CLI 未启动或已结束但返回不可用时，同轮重试一次，连续两次异常后自动沿计划阶梯切换到有额度的下一执行者；无下一执行者才转负责人。仍在运行则等待，状态不明不自动重派。历史协议和其他角色仍在连续 `invalid_retry` 次异常后标记 `ops` 暂停；前端读故障原文后用 `retry WO-n --note T` 或 `escalate WO-n --note T` 留下处置。
- 前端可读任何文件、运行只读命令和已配置的账号切换命令；写入只限 `answers/` 下的新文件；不改代码、不做技术判断。

## 修复历史登记（reconcile）

协调调用不占技术轮次；report.recent 的 budget_charge 区分 technical、coordination、pending、exempt、credited。调用记录仍保留。当前有在途工作时禁止迁移。

只有已获用户授权的历史修复才使用：

```text
bf/cli.py --base <任务>/.batch-flow reconcile --file <base>/answers/repair.json --approval <base>/answers/user.txt --dry-run
bf/cli.py --base <任务>/.batch-flow reconcile --file <base>/answers/repair.json --approval <base>/answers/user.txt
```

清单包含 revision（当前状态修订）、order、paths（精确路径）、prior_approvals（已批准同目标问题）、credits（每项 run 与 reason）、resolve_asks（原单的空动作升级问题）。原话文件须包含用户对本次修复的授权，操作人核对先前批准确实覆盖这些路径和内容，不能从 approve=true 猜测任意路径授权。

恢复时可附 constraints（原单额外执行约束）、fresh_session（明确换新执行上下文）；execution_notes 仅作历史记录，不再自动进入派发正文。这只澄清执行方式，不改验收、范围、预算、依赖、审阅或宿主授权；不能借操作说明夹带新承诺。原始用户批准和清单均留哈希。负责人整理并替换当前有效约束，旧报告保留，不累加覆盖语句。

脚本同时检查当前契约、已有批准、运行证据完整性、冲正轮次没有源码变化或受控入口执行、修复后权限和执行余额均成立。原运行不改写，冲正另存 budget_credits；清单和批准记录哈希，精确重复请求幂等，dry-run 不修改状态。不能用于冲正真实实施或测试、跳过审阅、增加总预算或扩大验收。迁移不会启动任务，也不会解除 stop；技能维护结束不自动恢复项目编排。

新问题不再使用这种迁移方式。计划声明 required_protected，裁决用 authorize 给原单登记权限；关联 grant、retry 与 authorize 整包批准执行。纯文字答复转协调解释，不能充当结构化权限登记。

## 通知、体检与立即停止

- `watch [--since N] [--once]`：把值得关注的事件（驱动器启停、待你决定、运维暂停、交付结果、批次放行、停止）逐条输出为一行中文；前端用 Monitor 挂着它。`report` 的 `driver.alive` 表示驱动器是否在运行。
- `doctor`：列出任务目录里引用了、但技能中已不存在的脚本（`{"missing": [{file, line, ref}]}`）；切换或升级技能前运行。
- `stop` 同时阻止续跑（不合法交付暂存，`resume` 后继续）；`stop --now` 先向启动回执中核验仍属于本次进程组的实际 CLI 发送 SIGINT（对应 Ctrl+C），等待其正常取消与工具清理，保留监督进程回收结果。各后端共用该流程；超时或没有有效启动回执时再按进程组 TERM/KILL 兜底。正常中断能力须按后端验证；强制兜底不保证清除已脱离进程组的后台任务，怀疑残留时需检查宿主进程，不能仅凭 running=0 判断全部停止。在途调用登记为无效，驱动器退出；新版已启动技术调用待负责人结算。

## 判断职责调整后的维护

快照/证据哈希变化与文档差异不自动阻止派发或放行；report 保留 broken_evidence，派发工单包含注册 SHA、当前 SHA、缺项和版本历史，由审阅者或负责人判断。显式 stop、hold、预算、权限、资源锁和独立审阅继续有效。
维护升级不会修复旧 ops 或旧工单指令；原有暂停、历史结果和批准保留。现存任务需按支持的 bf 操作核查并修复登记后另行恢复，不自动 ensure-run。
每次首轮派发保存 dispatch.json 与 card-1.md，全部角色使用统一有效输入；续作引用原卡，导出丢字段在模型启动前报工具错误。

### 仅修复历史协调记账

reconcile 支持 paths=[] 且 credits 非空：只审计冲正指定的纯协调误记，不安装任何路径权限，不夹带 resolve_asks 批准或 execution_notes 计划变更。仍须本次修复的用户原话文件、同目标既有批准、当前 revision、完整且未漂移的运行证据；真实源码改动、受控测试及已有冲正不得再冲正。仅此模式可引用同目标旧契约批准作为历史来源，不能借此授予新权限。先 dry-run 核对，原运行与总额度保留。
前端帮助使用完整绑定命令 `<Python> <技能>/scripts/bf/cli.py --base <绑定目录> reconcile --help`；不支持 flow 简写或任意 Python heredoc。读取状态用 Read/Grep/cat，工单在 orders.json 的 orders 字段中。report.ops 不应转成用户选择，只有 report.ask_user 是当前待决。


新版技术调用恢复与结算见 [技术负责人](technical-lead.md#执行恢复与执行者选择)。CLI 异常的有限重试和阶梯切换不依赖选人裁决；未结算占原角色额度，结算不成为下一角色或审阅的前置条件。历史协议和运维记录保留，不自动迁移或重启，旧阻塞不会因技能升级自行解除。

驱动器按 run/segment 在同一事务保存交付或 CLI 异常回执。重启按回执恢复待启动续作或一次异常重试，不重复回收或重置重试次数；启动交接若状态不明则交负责人，不冒充未启动后自动重派。inflight 原子写入，启动意图先于实际启动保存。
