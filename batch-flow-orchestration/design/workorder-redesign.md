# batch-flow v2：工单驱动重构设计（第 2 稿，2026-11-07）

状态：待用户审阅。本文是重构的设计与验收依据，不是运行规则；SKILL.md 不链接本文。实施完成后，规范内容写入 v2 的协议与角色文档，本文保留为设计记录。

已定方向（用户 2026-11-07）：
- 工单是唯一的状态对象，不保留“台账 + 工单”双轨；
- 彻底重构，不为兼容旧结构保留不合理设计；旧台账一次性导入；
- EtherCAT 在重构完成后以工单方式重新执行 S15.E；
- 由技术负责人（Opus）先写设计与不变式测试，Sonnet 实现，Opus 审查；三个阶段连续推进。

## 1. 原则

技能的初衷：**在大多数情况下无需人工干预，由角色配合（含裁决机制）按计划目标推进任务。** 除此之外唯一需要专门设计的是**上下文切换时机**。

据此，每一项机制都按三个问题检验，答不出就删：

1. 它是否帮助任务在无人干预时继续推进，或在必须停下时停在正确的人面前？
2. 它是否保护一条用户明确要求的边界（预算、范围、验收、独立审阅、证据）？
3. 它是否决定了“谁在哪个上下文里做下一件事”？

## 2. 现状问题（量化，EtherCAT 台账 rev 697）

| 项 | 现状 | 判断 |
|---|---|---|
| flow.json | 6.8 MB，每次 status/next/stop hook 整份读入校验 | 热路径过重 |
| history | 2.5 MB（74%），33 次 plan_update 各存完整计划 | 审计日志放在热文件 |
| attempts | 139 条，每条复制任务快照与配置 | 重复存储 |
| tracking | 260 KB 统计数据在热文件 | 应由日志计算 |
| 计划 | 81 任务、93 个预算条目、27 个 subject | 预算逐任务列举 |
| 操作 | 18 种，`operate()` 约 450 行特例 | 生命周期不统一 |
| 脚本 | flow.py 2165 行、dispatch_card.py 1611 行 | |
| 协议文档 | 约 225 KB | 角色负担重 |
| 编排者 | 模型会话 + 受限启动器、guard、stop hook、角色审计、计时器 | 为约束一个“不该做技术工作的模型”付出大量机制 |

2026-11-07 暴露的派发问题（提前结束、环境误判、裁决未进卡、待裁决边界、候选项丢失、组审阅整组返修、返修卡无审阅结论、返修继承实施升级阶段）的共同根因：一轮工作的来源与内容没有结构化表示，各类事项各走特例路径。

## 3. 不变式（v2 必须保持，每条有测试）

| ID | 不变式 |
|---|---|
| I1 | 状态只经管理脚本写入；排他锁、CAS、请求幂等、原子替换 |
| I2 | 预算只按策略消耗，不重置、不因重建增加；增加须用户批准 |
| I3 | 实施阶梯：主执行轮次用尽 → 备用一次 → 负责人兜底（用户 2026-11-07 裁定） |
| I4 | 审阅独立：工单作者的上下文不得审该工单的交付 |
| I5 | 交付成功须引用存在且哈希完好的证据，并绑定固定版本 |
| I6 | 用户停止优先；超出用户授权包络（预算、范围、验收）的事项必须停在用户面前 |
| I7 | 下一步做什么完全由状态文件推导，驱动器重启后可恢复 |
| I8 | 交付是否合法只由脚本按结构判定，不调用模型 |
| I9 | 已发生的记录只追加，不改写 |

旧版中“编排者不做技术工作”“dispatch_owner 唯一”等约束，在 v2 中由“驱动器是脚本”从结构上消除（§6），不再需要规则与门禁。

## 4. 文件与数据模型

```text
<任务目录>/.batch-flow/
  plan.json      目标契约、交付目标、角色模型、额度策略、上下文策略、环境配置
  orders.json    全部工单（开立中完整，已关闭保留摘要）——唯一的热状态
  events.jsonl   只追加日志：每次写操作的 actor、时间、请求、结果哈希
  records/       证据（同现行）与派发卡目录
  plan.history/  计划各修订的完整副本（按修订号存文件，不进热文件）
```

### 4.1 plan.json

```text
goal:        项目目标与约束（现 goal_contract）
deliverables: {id: {batch, title, acceptance, depends_on, design_ref, instruction}}
roles:       {executor, backup_executor, lead, reviewer, third_party: {tool, model, effort}}
policy:
  rounds:    {implement: 3, backup: 1, lead: 1, repair: 2, review: 3}   # 每个交付目标的默认额度
  overrides: {<deliverable>: {...}}                                     # 个别调整，需用户批准
  context:   §9 的可调参数
env:         {<backend>: {require_host, add_dirs, approval_policy, entry, preflight, blocked_output, guide}}
envelope:    用户授权包络：可自主推进的批次范围、总额度上限
design:      {path, sha256}
revision:    计划修订号
```

交付目标取代现行“任务”：只描述要交付什么、如何验收、依赖谁。审阅、放行不再是计划里的独立任务，而是由规则自动开立的工单（§5.3）。

### 4.2 orders.json 中的工单

```text
{
  id:      WO-0001（顺序号）；key 字段存来源键用于幂等，如 repair:WO-0012:B1
  type:    implement | review | repair | decide | ask_user | triage | release
  target:  交付目标 ID（decide/ask_user 可为空）
  origin:  {by: plan|order|user, ref, reason}
  input:   本单要做的事（结构化，§10）
  needs:   [前置工单 ID]
  runs:    [{run_id, performer: primary|backup|lead|reviewer|..., tool, model, session,
             started, ended, result, stop_reason, evidence:{path: sha256}, version, card}]
  state:   open | running | done | failed | superseded
  output:  最后一次合法交付的结构化摘要
}
```

运行记录（run）在工单内部，不再有全局 attempts；同一轮内续跑是同一 run 的多段调用（§10.3）。

## 5. 工单类型与路由

### 5.1 类型（7 种）

| 类型 | 处理者 | 完成条件 |
|---|---|---|
| implement | 执行者阶梯（I3） | 合法交付 VERIFIED |
| review | reviewer / third_party | RELEASE；FIX_REQUIRED 时开立 repair 并在复审后完成 |
| repair | 返修链（§8） | 所列阻断项经复审关闭 |
| decide | 技术负责人 | 产出结构化处置（§7） |
| ask_user | 用户 | 用户回答登记 |
| triage | 技术负责人 | 候选项逐项有去向 |
| release | 脚本（机械） | 批次内交付目标全部完成、审阅通过、无未分诊候选项 |

### 5.2 路由表（事件 → 工单），全部由脚本确定性执行

| 事件 | 结果 |
|---|---|
| 计划批准 / 修订 | 为新交付目标开立 implement；按 policy 为每个 implement 开立 review（needs implement） |
| implement 合法 VERIFIED | 工单完成；其 review 变为可派发 |
| implement 合法 FAILED（技术不可行） | 同工单按阶梯下一轮；阶梯用尽 → decide |
| implement 合法 待裁决 / 外部阻塞 | 开立 decide（附交付与证据），implement 等待其处置 |
| 任一不合法结束 | 同一 run 续跑（§10.3） |
| review FIX_REQUIRED | 按阻断项的“返修”目标分组，每个目标开立一张 repair；未点名目标不动；review 等待这些 repair |
| repair 交付 | review 复审，只核对所列阻断项与回归 |
| 复审某阻断项仍未关闭 | 该阻断项的 repair 升链（§8）；链用尽 → decide |
| 任一交付含候选新项 | 追加到所属批次 triage |
| decide 产出 | 按处置开立/作废工单，或开立 ask_user（§7） |
| 批次全部完成 | release；之后下一批交付目标可派发 |

### 5.3 审阅组合

现行“一次审阅覆盖多个任务”（如 S1314.R）保留为 plan.policy 中的审阅分组；review 工单的 needs 包含组内全部 implement，返修仍按阻断项分到具体目标。

### 5.4 选择下一张工单

工单列表是动态的：每次登记（交付回收、裁决处置、用户回答、计划修订）都按 §5.2 当场开立、完成或作废工单。驱动器不“规划”，只在每次有事件后从列表中选：

1. 可派发条件（全部满足）：state=open；needs 全部 done；所属交付目标的 depends_on 全部完成；同一交付目标上没有未完成的 decide；所属批次在 envelope 内；所需资源（§6.4）空闲；运行中工单数未达并发上限。
2. 优先级（确定性）：decide → repair → review → implement → triage → release。先解除阻塞，再收尾在途工作，最后才开新工作，避免同时摊开过多半成品。
3. 同级按交付目标在计划中的顺序（批次序、再目标序），再按工单号先后。
4. ask_user 不派发：写入 attention 并通知；只阻塞依赖它的工单，其余照常推进。

驱动器是事件驱动的：派出工单后等待进程结束（或 `bf answer`、`bf stop` 信号）再重新选择，不做定时轮询。

## 6. 驱动器：脚本，而不是模型编排者

### 6.1 方案

`bf run` 是一个普通进程，循环执行：读状态 → 选出可派发工单 → 渲染卡片 → 经 agent_cli 启动角色 CLI → 等待 → 回收、门禁、登记 → 按路由开立后续工单。没有可派发工单时：若有 ask_user 则通知用户并等待；若全部完成则退出。

- 所有角色都经 CLI 适配器调用（codex、claude -p、opencode、agy），模型与额度由 plan.roles 指定。
- 并发：同一交付目标同时只有一张工单在运行；哪些目标可以并行在计划阶段确定（§6.4），运行时 `bf run --parallel N` 决定是否使用，默认 1。
- 用户交互：`bf status`、`bf report`、`bf answer <工单> <原话文件>`、`bf stop`、`bf approve-plan`；通常经聊天前端使用（§6.3）。停止信号由驱动器在调用间检查；正在运行的 CLI 可按需终止。
- 通知：ask_user 或驱动器停止时写入 `attention.md`，可选执行用户配置的通知命令。

### 6.2 由此删除的机制

模型编排者会话、Claude Code / AGY 受限启动器、PreToolUse guard、Stop hook（pre_yield）、角色审计、编排者计时器与 register_dispatch、dispatch_owner、“编排者不得做技术工作”的规则与相关文档。约 40 KB 宿主文档与 4 个脚本退出主路径；聊天前端只保留命令白名单（§6.3）。

### 6.3 聊天前端（用户 2026-11-07 决定保留）

保留一个轻量模型会话作为用户的对话入口，职责只有三项：

1. 启动、停止、查看 `bf run`（后台进程）；
2. 每 10 分钟（及用户询问时）汇报进展：运行 `bf report` 取结构化摘要（在途工单、各运行耗时与最后动作、新完成/新开立的工单、ask_user、额度余量），再用自然语言转述；
3. 转交用户回答：用户口述的决定由前端写成原话文件，经 `bf answer` 登记。

前端不选择、不派发、不回收工单，也不读写状态文件；它只能运行 bf 的查询与上述三类命令。因为调度不在它手里，旧版为约束编排者而设的 guard、stop hook 门禁、角色审计都不再需要；前端只保留“命令白名单”这一项限制。驱动器不依赖前端：前端会话结束或上下文压缩，`bf run` 照常推进。

角色仍全部经 CLI 调用；失去的是宿主原生子代理（Claude Agent 工具、AGY 原生子代理），Claude 角色经 `claude -p` 使用同一订阅。

### 6.4 并发与资源

计划阶段声明：交付目标之间的 depends_on，以及每个目标使用的独占资源（如 `resources: [board]`）。运行时只有在 `--parallel N>1` 时才并发，且两张需要同一独占资源的工单不会同时运行。默认串行，与现状一致。

## 7. 裁决机制（简化）

一个 decide 工单，输入为触发事件与证据，技术负责人产出一份结构化处置 `decision.json`：

```text
{
  diagnosis: <一段文字>,
  actions: [
    {open: implement|repair|review|triage, target, input, needs?, fresh_session?},
    {supersede: <工单>, reason},
    {retry: <工单>, reason}                       # 环境已恢复，原轮次续作
    {plan_revision: <修订草案路径>}               # 改验收/范围/交付目标
    {grant: {<工单或目标>: {rounds: +n}}}         # 增加额度
    {ask_user: <问题>, options?}
  ]
}
```

规则：
- 处置在 envelope 内（不加额度、不改验收与范围）→ 脚本直接执行，decide 完成，任务继续推进。
- 含 plan_revision、grant 或超出 envelope → 自动开立 ask_user，用户批准后执行（I6）。
- decide 自身用尽负责人额度或处置无效 → ask_user。

由此删除：signal 的多种类型与 decision_points、恢复条件（requirements）与 confirm/close 双步、clarification、repin、supersede_decision、confirmation_waits、classify_attempt / resume_attempt 的独立操作、用户离开模式（自主推进是默认，envelope 即授权边界）。

环境失败的免扣：run 未产生任何文件改动和证据记录、且门禁判定为外部阻塞时，脚本自动标记该 run 不计额度（可核验的机械条件）；其余情况一律计入。

## 8. 执行人与额度

| 工单 | 执行人链 | 额度 |
|---|---|---|
| implement | primary（rounds.implement）→ backup（1）→ lead（1） | 按交付目标计 |
| repair | primary 新会话（rounds.repair）→ backup → lead；“方案级”阻断项直接 lead | 按 review 周期计，不继承 implement 已升阶段 |
| review | reviewer；第三方按 policy | rounds.review |
| decide / triage | lead | 不计技术轮次；同一触发源重复 decide 有上限（默认 2），超出 → ask_user |

额度用尽的唯一出口是 decide；decide 的唯一升级出口是 ask_user。

## 9. 上下文策略（一等设计）

上下文是否续用由一个函数 `context_for(order, performer)` 决定，规则全部列在这里，可由 plan.policy.context 调整阈值：

| 情形 | 决定 |
|---|---|
| 同一 run 内续跑（门禁退回） | 续用同一会话（必须） |
| 同一 implement 的下一轮、同一执行人 | 续用，除非：上一轮会话记录超过阈值（默认 300 KB）、或 decide 要求 fresh_session、或上一轮被判定为环境误判 |
| 执行人阶段变化（primary→backup→lead） | 新会话 |
| repair | 新会话，输入为阻断项与审阅结论（不续用实现会话，避免锚定旧方案） |
| review 首审 | 新会话，且不得是该交付任一 run 的会话（I4） |
| review 复审 | 续用首审会话（熟悉上下文），超阈值则新会话 |
| decide | 同一交付目标链上的 decide 续用负责人上一个决策会话；该会话若是被裁决对象的作者则新会话 |
| triage | 新会话 |
| 跨批次 | 新会话 |

每次派发在 run 里记录 `session` 与选择理由（规则编号），便于事后分析上下文切换是否合适。

## 10. 卡片、交付与门禁

### 10.1 卡片（由工单渲染，固定结构）

1. 身份与目标（goal、交付目标验收）
2. 环境（沙箱事实、受控入口、preflight）
3. 本单要做的事（按类型：指令 / 阻断项与审阅结论 / 被审版本与待核对项 / 触发证据 / 候选项清单）
4. 约束（来自 decide 的一次性要求）
5. 交付结构

### 10.2 交付结构（脚本只解析固定行）

执行者：`结果`、`停止原因`、`改动与版本`、`验证证据`、`未运行/失败`、`上升问题`、`阻断关闭`（repair）、`候选新项`、`限制`。判定规则写在模板中：待裁决 = 继续需改动本卡无权改动的内容（验收、冻结资产、范围、他批接口、预算权限、系统资源门限）；范围内仍有可试路径 → 未完成。

审阅者：`结果`、`固定版本`、`复跑与证据`、`阻断：B1 | 返修:<目标> | 方案级:是/否 | 关闭条件:...`、`非阻断建议`、`候选新项`。FIX_REQUIRED 缺结构化阻断行 → 门禁退回审阅者续跑。

技术负责人：`decision.json`（§7）；triage 输出逐项去向。

### 10.3 门禁与续跑

沿用 5311160 的规则，推广到所有角色：不合法结束在同一会话、同一 run 内续跑，最多 2 次，第 2 次要求有进展；用尽后按“技术不可行/FAILED”计入，路由照常。

## 11. 用户介入点（仅此四类，均可经聊天前端完成）

1. 批准计划（即授权 envelope）；
2. 回答 ask_user（超出 envelope 的处置、负责人无法处置的问题）；
3. 停止 / 恢复驱动器；
4. 可选：查看状态与 10 分钟汇报。

其他一切由角色配合推进。

## 12. 删除与合并清单

| 旧机制 | v2 |
|---|---|
| 18 种操作 | 工单动词：open / start / deliver / close / supersede，外加 plan approve/revise、answer |
| 93 个预算条目、handoff 配置 | policy.rounds + 个别 overrides |
| subjects、change_subject、subject_versions | run.version（交付时的 git 引用或快照） |
| decision_points、signal 类型、requirements、confirm/close、repin、supersession、clarification | decide 工单 + decision.json |
| confirmation_waits、legacy waits | 删除（ask_user 工单） |
| classify_attempt、resume_attempt、resumptions、environment_retry_limit | 门禁机械免扣 + decide 的 retry |
| authorize、requires_authorization、user_mode 离开模式 | plan 批准即 envelope |
| REPORT_PROGRESS、feedback_minutes、计时器、register_dispatch | 驱动器自身监控进程 |
| tracking（热文件） | 由 events.jsonl 计算 |
| design 节哈希、SYNC_DESIGN、design_refresh | design.sha256 与 plan 修订绑定；不一致 → triage 一项 |
| simulation / history 回放 | 删除 |
| context_groups、session_policy、context_hint、author_contexts | §9 单一函数 |
| 模型编排者及其宿主机制 | 驱动器脚本 + 只做汇报与转交的聊天前端（§6） |

## 13. 代码结构与规模目标

```text
scripts/bf/
  store.py      锁、CAS、幂等、原子写、events.jsonl
  model.py      plan 与工单的结构校验
  routing.py    事件 → 工单（§5.2）
  policy.py     执行人链、额度、免扣（§7、§8）
  context.py    上下文策略（§9）
  cards.py      卡片渲染与交付模板
  gate.py       交付解析与门禁、续跑
  runner.py     bf run 主循环
  cli.py        bf status / report / answer / stop / approve-plan / record / snapshot
  front.py      聊天前端启动器与命令白名单（替代旧启动器、guard、hooks）
  importer.py   v1 → v2 一次性导入
scripts/agent_cli.py      保留（CLI 适配器，已验证的宿主细节）
```

目标：核心代码 ≤ 2000 行（现 flow.py + dispatch_card.py + handback ≈ 3900 行）；orders.json 对 EtherCAT 量级 ≤ 500 KB；`bf status` ≤ 100 ms；角色文档每份 ≤ 1 页，协议文档总计 ≤ 60 KB。

## 14. 导入与切换（EtherCAT）

- 时机：重构完成、验收通过后。当前 S15.E 由旧系统继续运行至结束或停止；切换后以工单重新执行 S15.E。
- 导入：
  - plan.json 由旧计划转换：交付目标取 S15–S20 及其后续未完成项；已完成项作为 done 的 implement 工单保留证据引用（供依赖判定）；
  - 剩余额度按旧台账实际消耗换算后写入 overrides；
  - 未完成的 S15.E 开立为新的 implement 工单（上下文新开，输入附旧系统中最后一次交付）；
  - 旧 flow.json 与 records 原样归档、只读。
- 切换前演练：在副本上导入，`bf status` 与 `bf run --dry-run` 的首批派发与预期一致，由技术负责人核对，用户确认后切换。

## 15. 实施与验证

一次连续推进，按顺序完成，阶段间由 Opus 审查通过即继续，不等待用户：

| 阶段 | 内容 | 先行测试（Opus 编写） |
|---|---|---|
| A | store、model、routing、policy、context、gate；导入器 | §3 不变式；§5.2 路由逐行；§8 额度与阶梯；§9 上下文表逐行；门禁用例（复用 5311160 用例） |
| B | cards、runner、cli；角色 CLI 端到端（用假 CLI） | 驱动器重启恢复、停止、ask_user 等待、续跑、并发上限 |
| C | v2 文档（SKILL、角色、协议）；导入 EtherCAT 副本演练 | 文档结构与链接校验；导入演练报告 |

实现由 Sonnet 在独立 worktree 完成；Opus 审查每阶段差异并复跑全部测试。旧脚本在切换前保持可用（新代码在新目录，不修改旧路径）。

## 16. 用户决定记录

1. 驱动器：脚本 `bf run` + 保留聊天前端，负责 10 分钟汇报与转交回答（§6.3）——已定。
2. 默认额度：implement 3 / backup 1 / lead 1 / repair 2 / review 3，同一触发源 decide 上限 2——已同意。
3. 候选新项分诊位置：每批 release 前——待确认。
4. 并发：计划阶段声明依赖与独占资源，运行时选择是否并发，默认串行（§6.4）——已定。

## 17. 阶段 A 接口契约

以 `scripts/test_bf_core.py` 为准（测试即规格）；Python 3.8，仅标准库。包 `scripts/bf/`，对外入口 `bf.api`：

| 函数 | 作用 |
|---|---|
| `init(base, plan, approval)` | 校验计划，写 plan.json / orders.json / events.jsonl；为每个交付目标开 implement，为每个审阅组开 review，为每个批次开 release |
| `load(base) -> State` | `.plan`、`.orders`（id→工单）、`.revision`、`.stopped` |
| `orders(state, type=None, target=None, state_=None)` | 按工单号排序的列表 |
| `runnable(state, parallel=1)` | 可派发工单 ID，按 §5.4 优先级；release 也在其中 |
| `assign(state, order_id)` | 纯函数：`{stage, role, tool, model, effort, session:{mode, conversation_id, rule}}`；额度用尽抛 `BfError` |
| `start(base, order_id, conversation_id=None, expected_revision=None, request_id=None) -> run_id` | 按 assign 登记 run |
| `deliver(base, order_id, run_id, text, events=(), decision=None, conversation_id=None, session_kb=None, ...)` | 门禁 + 路由；返回 `{legal, reason, outcome: continue/registered, opened:[...]}` |
| `release(base, order_id)` | 机械放行 |
| `answer(base, order_id, text, approve)` | 用户回答 ask_user |
| `revise_plan(base, plan, approval)` | 计划修订（需用户原话文件） |
| `stop(base)` / `resume(base)` | 用户停止 / 恢复 |
| `broken_evidence(state)` | 哈希不符的证据列表 |

错误一律抛 `bf.api.BfError`。

## 18. 阶段 B 接口契约

以 `scripts/test_bf_runner.py` 为准。

| 对象 | 约定 |
|---|---|
| 角色适配器 | `launch(order, assignment, card_path, run_dir, resume=None) -> handle`；`poll(handle) -> None | {conversation_id, text, events, session_kb?, decision?}`。真实适配器包装 `agent_cli.py`：`events` 由 CLI 原始输出解析，`decision` 读自 `run_dir/decision.json` |
| `runner.Runner(base, adapter, parallel=1)` | `step()`：回收已结束的调用 → 门禁登记（不合法则以续跑提示续用同一会话）→ 机械放行 → 按 `runnable` 派发；`run_until_idle()`：反复 step 直到无在途且无可派发；每次派发前检查 stopped |
| 重启恢复 | 启动时发现无句柄的在途 run：`api.abandon(base, order_id, run_id, reason)` 登记为 INVALID、免扣，然后照常重派 |
| 卡片 `cards.render(state, order_id, assignment) -> str` | 固定节：`## 目标`、`## 环境`、`## 本单`、`## 约束`、`## 交付`；续跑卡为短提示（< 800 字），以“尚未合法结束”开头说明原因 |
| attention | 有开立的 ask_user 时写 `<base>/attention.md`，列出工单号与问题 |
| `api.report(base)` | `{running, recent, ask_user, budgets{deliverable: 剩余}, runnable}` |
| `bf/cli.py --base <dir> <cmd>` | `status`、`report`、`run [--parallel N]`、`stop`、`resume`、`answer <id> --file <原话> --approve|--reject`、`init --plan <json> --approval <file>`、`revise --plan <json> --approval <file>` |
| `front.allowed(command) -> bool` | 只放行 `<python> <bf/cli.py> --base <dir> {status, report, run, stop, resume, answer}` 的单条命令，拒绝管道、重定向、分号、其他脚本与写操作子命令 |
| triage 候选项 ID | 每张 triage 工单内按出现顺序编号 `C1, C2, …` |
