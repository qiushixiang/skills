# 授权传达、工单处置完整性与任务环境拆分（提案，2026-11-12，待用户审）

来源：`0-ethercat_reliability` 在 11-11 获得较大授权并 unhold 后停滞约 9 小时。本提案只处理暴露出的三个设计缺陷，
不在现有判断分支上追加特例。用户已定方向：A1、A2 按建议，A3 改为“CLI 用法与任务环境分离”，不能只加校验。

## 0. 事实回顾

| 现象 | 直接原因 | 设计缺陷 |
|---|---|---|
| unhold 后 R3–R6 执行者仍只做离线核对，负责人连判 technical_block | 计划 `env.*.guide`、`deliverables.*.constraints`、design.md 仍写“当前仅离线窗口 / 暂缓” | 临时状态写在角色读取的正文里；结构化调整（unhold）不进入卡片，也不触发依赖它的裁决重开（A2） |
| WO-0004/5/6 停在“等待处置”，既不可派发也不出现在受阻清单 | 前端对 technical_block 用 `retry`：删掉 ops、未清 `awaiting_disposition`、未开 decide | 工单没有“唯一可推进出口”的不变式；前端动词不按问题归属受限；report 读原始字段而非推导状态（A1） |
| 主执行者 7 次全部启动前失败 | 计划 `env.opencode` 复制了 codex 的 `approval_policy` | 计划里直接写后端参数，固定部分与任务环境部分没有拆开；校验只在派发时进行，负责人设计阶段没有解析与冒烟环节（A3） |
| report 仍显示已 superseded 的 WO-0007 受阻 | `report.ops` 直接枚举带 `ops` 字段的工单 | 同 A1：展示未从统一处置状态推导 |

## A1. 工单处置完整性

### 不变式（新增 I-D）

每张非终态工单（非 done/superseded）在任一事务提交后，必须恰好处于下列一种**处置**，且每种处置有确定的推进者：

| 处置 | 判定 | 推进者 |
|---|---|---|
| running | 有未结束运行 | 驱动器 |
| runnable | 现有 runnable 条件成立 | 驱动器 |
| waiting_needs | 存在未满足的 needs（含裁决单） | 被依赖工单 |
| ops | 有 ops 且 `ops.owner ∈ {front, lead}` | 对应角色 |
| ask_user | 存在本单的未答 ask_user | 用户 |
| held / stopped / outside_envelope | 计划 hold、全局 stop、不在授权批次 | 用户 |
| exhausted | 额度用尽且已有对应裁决或用户问题 | 见其 needs |

- 用一个纯函数 `disposition(state, order)` 计算，`runnable`、`report`、`status`、前端汇报都从它派生，不再各自读
  `ops`、`awaiting_disposition` 等原始字段。
- `transact` 提交前对全部工单求值。任一工单无处置或同时落入两类的，整笔事务失败，状态不落盘。
  这样 WO-0004 这类“删 ops、留 awaiting_disposition、无 decide”的写法在源头就被拒绝。
- `awaiting_disposition` 不再是独立标记，改为“needs 中存在本单未完成的裁决单”这一事实的推导。
  删除该字段，结算逻辑改看裁决单。旧库导入时，凡有该标记但没有未完成裁决单的，由导入为其开一张裁决单，并在报告中列出。

### ops 归属与前端动词

- `ops` 增加 `owner`：`front` 表示运维故障（CLI 不可用、账号、额度结算等机械问题），`lead` 表示技术受阻
  （technical_block、no_progress 等由负责人产生的问题）。
- 允许的动作由脚本按 owner 给出，前端只能从中选：
  - `owner=front`：`retry`（清除后重试）、`escalate`（转负责人）。
  - `owner=lead`：前端只能 `reopen --note`，即带运维事实（例如“用户已协调资源”）重开负责人裁决。不能 retry。
- `retry`、`escalate`、`reopen` 都必须让工单落到新的合法处置，由 I-D 保证，不再靠各动词自行记得清理字段。

### 测试

- 性质测试：对现有测试库的全部 API 序列逐步断言 I-D。
- 回归用例：复现“technical_block 后 retry”，应被拒绝；在 owner=lead 上 reopen，应生成裁决单。
- report 用例：superseded 工单不进受阻清单；“有 awaiting 但无裁决单”的旧库导入后可见。

## A2. 授权与临时状态的传达

### 原则

计划正文（design、instruction、constraints、env 说明）只写**稳定的规则与条件**，不写“当前是否暂缓、窗口是否开放、
资源是否已释放”这类**会变化的状态**。状态只存在于结构化字段（holds、adjustments、grants、stop）。
角色看到的状态一律由脚本在派发时从结构化字段导出。

### 机制

1. **卡片“当前生效状态”节**：派发时导出与本工单相关的结构化状态及其批准来源：所属批次与目标的 hold 状态、
   最近的 unhold/hold/grant 调整（时间、批准文件路径与 SHA），以及本单额度。正文与该节冲突时，以该节为准，
   卡片中写明此规则。
2. **hold 携带条件，正文按 ID 引用**：`hold` 增加 `id` 和 `reason`（例如“另一任务占板”），正文只写
   “受 hold `board-window` 约束时不得…”。解除后，正文无需修订即可自然失效。
3. **受阻记录依赖的事实**：负责人给出 technical_block 或 blocked 时，可在结构化字段 `waits_on` 中列出
   hold ID、额度、计划修订等可机械判定的条件。其中任一条件变化（unhold、grant、revise）时，脚本自动为受影响工单
   重开负责人裁决，并把变化事实写进裁决卡。这条机制直接覆盖“已授权但没人重新判断”的情形。
   `waits_on` 只能取可机械判定的项，文字理由仍由负责人判断。
4. **编制规范**（references/plan.md 与 design-book.md）：明确第 1 条原则，并给出“占板分窗”的标准写法
   （hold + 条件）作为样例。编制规范只是辅助；即使正文写错，第 1、3 条机制也能纠正。

### 测试

- 卡片导出：hold 和 unhold 之后，卡片的“当前生效状态”节准确反映状态和批准来源。
- `waits_on`：被 hold 阻塞的 blocked 工单在 unhold 后自动获得新的裁决单。不带 `waits_on` 的不自动重开。

## A3. CLI 调用：固定部分 + 任务环境部分（第 2 稿，按用户 11-12 更正）

用户更正：CLI 调用由两部分组成。**固定部分**跨任务不变；**环境部分**随任务变化，例如某任务需要放宽沙箱，
或让受控入口在宿主执行，这正是此前 Codex 板测 EPERM 遇到的问题。拆分和校验都必须做，
并且要让负责人在设计阶段落实，不能到派发时才暴露。

### 三层模型

| 层 | 内容 | 归属 | 谁改 |
|---|---|---|---|
| 固定部分 | 每个 (backend, access) 的基础调用：输出格式、续接、正文传输、effort 映射、默认沙箱 | 适配器 | 技能维护，经真实接入验收 |
| 环境需求 | 任务**需要什么**，用与后端无关的词表达：宿主执行的受控入口、额外目录、沙箱级别、网络、审批行为等；每项注明理由和适用范围（目标 × 读写） | `plan.env.requirements` | 负责人编制，用户批准 |
| 后端映射 | 每项需求在各后端**怎么实现**，例如 Codex 用 rules `prefix_rule` 加宿主父进程、`approval_policy`、`sandbox_mode`；OpenCode 和 Claude 用各自的 permission 或 settings；实现不了标 unsupported | 适配器能力表 | 技能维护；某后端新增映射须经真实验证 |

- 计划中**只写需求，不写后端参数**。`approval_policy`、`sandbox_mode` 这类键不能出现在计划里，
  只能由能力表在映射时生成。抄错后端参数的情况因此在结构上无法发生。
- 确实需要后端专属调参时，只能使用能力表声明为 `tunable` 的项，并且必须写明理由，经用户批准。
  未声明的键一律拒绝。

### 让负责人在设计时落实

1. **编制必填**：`env.requirements` 是必填字段；没有需求时显式写 `[]`。
   design-book 增加“执行环境”节，固定为四列：需求、理由、适用目标与读写、授权与风险。
   plan.md 给出样例，以本任务的“板测入口须宿主执行”为例。
2. **解析器 `bf env-resolve --plan <草案>`**：按“角色 × 读写 × 目标”，用能力表和已探测的 profile，
   把需求解析成每次调用实际会得到的参数、规则和设置，并输出两部分：
   - 每个角色实际得到的环境：沙箱、宿主入口、目录、审批方式。
   - 失败清单：后端不支持、键未知、tunable 缺理由、规则文件不在宿主实际加载路径、profile 不能证明某开关可用等。

   负责人起草时自行运行。`init`、`revise`、`set-role` 内部调用同一个解析器，有失败就拒绝登记。
3. **登记时冒烟（事实校验，设计阶段完成）**：`bf env-resolve --plan <草案> --smoke` 对每个
   “角色后端 × 读写 × 需求集合”组合，用解析后的真实参数各跑一次冒烟，回执写入 `<base>/records/env-smoke/`。
   回执内容包括解析哈希、profile 与 CLI 版本、规则文件 SHA、冒烟命令、退出码和原始输出。
   - 冒烟命令由需求项的 `smoke` 字段声明，必须**无副作用、不占用外部资源**。它只证明调用环境成立，
     例如宿主入口的 `selfcheck`：核对 NoNewPrivs、`sudo -n -l` 精确端点、规则命中、目录可写，
     不连接设备，不受板卡占用影响。入口没有这种模式时，负责人须先在设计中补上，否则该需求无法登记。
   - 后端支持无模型的沙箱执行时，直接执行冒烟命令；否则发一次最小模型调用，只执行该命令并回报退出码。
     这类调用记为设计开销，不计入任何目标轮次。
   - `init`、`revise`、`set-role` 要求每个组合都有与当前解析哈希一致且通过的回执，缺一项或失败一项就拒绝登记。
     冒烟失败属于设计问题，由负责人在登记前解决或改分工，不会进入执行阶段。
   以前沙箱 EPERM、NoNewPrivs 这类问题只有实际执行才能暴露，本条把它们前移到设计阶段。
   按这个机制，本次 opencode 连续 7 次启动失败会在登记时就被拦下。
4. **解析结果入计划**：解析结果与冒烟回执引用写入 `plan.env_resolved`。驱动器派发时只做哈希比对，不做冒烟：
   - 一致：直接派发，不增加等待。
   - 不一致（profile、CLI 版本或规则文件在登记后变了）：只暂停受影响的“后端 × 读写”组合，
     记为 ops（owner=lead）。负责人重跑 `env-resolve --smoke` 并经 revise 更新登记后恢复；
     其他组合照常推进，不消耗实施轮次。
5. **用户批准的是实际效果**：计划批准材料附上“每个角色实际得到什么环境”及冒烟结果的表格，
   不让用户去审键名。放宽沙箱属于授权，必须在这张表里出现。
6. **换角色重新解析并冒烟**：`set-role` 换后端时重新解析并冒烟。新后端满足不了已批准的需求，就拒绝更换并列出缺口，
   由负责人改分工（例如板测目标只派给能宿主执行的后端）或请用户批准新的映射。
7. **设计审阅**：设计审阅组的检查项增加两条：执行环境节与各目标验收是否对应（例如需要板测的目标都有宿主入口需求）；
   放宽项是否限定在最小的目标与读写范围内。

### 迁移

- 旧计划中 `env.<backend>` 下的 `require_host`、`add_dirs`、`entry`、`preflight`、`blocked_output`、`guide`，
  转换为需求项和单一环境说明。`approval_policy` 按能力表反推为需求，例如 Codex write + on-request 对应
  “写入时自动审阅审批”，无法反推的拒绝并列出。
- 导入时运行解析器和冒烟。以 `0-ethercat_reliability` 为例：opencode 的“板测入口宿主执行”映射若尚未验证，
  会被列为 unsupported，交负责人处理（要么验证 OpenCode 映射，要么板测目标只派给 Codex）。
  这应当在 R3 之前完成，不能在派发时才发现。

### 测试

- 解析器：同一份需求在各后端得到固定参数；unsupported、未知键、tunable 缺理由都会被拒绝；
  `init`、`revise`、`set-role` 都会触发拒绝。
- 结构：计划中出现 `approval_policy` 等后端键时直接拒绝。
- 登记冒烟：缺回执、回执哈希过期、模拟 EPERM 失败时，init、revise、set-role 都拒绝；回执齐全时登记成功，驱动器不再冒烟。
- 冒烟命令：未声明 `smoke` 的需求拒绝登记；冒烟调用不计入任何目标轮次。
- 漂移：profile 或规则文件变化后，只暂停受影响组合（owner=lead），其他组合继续派发，不消耗轮次。
- 真实：Codex、OpenCode 各对一个“宿主入口”需求做一次真实冒烟。

## A3 实施规格（待审）

### 计划结构

```json
"env": {
  "requirements": [
    {"id": "host-parent", "kind": "host_parent", "reason": "受控入口需 sudo 端点",
     "applies": {"deliverables": "*", "access": ["write", "read"]}},
    {"id": "board-entry", "kind": "host_entry", "reason": "板测须在沙箱外运行",
     "entries": ["/abs/run_board_test.sh"], "preflight": "/abs/run_board_test.sh preflight",
     "smoke": ["/abs/run_board_test.sh", "selfcheck"],
     "applies": {"deliverables": ["R3.D", "..."], "access": ["write"]}},
    {"id": "caches", "kind": "extra_dirs", "dirs": ["/home/worker/.ccache"], "reason": "...",
     "applies": {"deliverables": "*", "access": ["write"]}},
    {"id": "escalate", "kind": "sandbox_escalation", "mode": "auto_review", "reason": "...",
     "applies": {"deliverables": "*", "access": ["write"]}}
  ],
  "blocked_output": "...",
  "guide": "与后端无关的环境说明"
}
```

- `requirements` 必填，没有需求时写 `[]`。`id` 唯一；`reason` 必填；`applies` 必填，取值为目标列表或 `"*"`，以及读写列表。
- `env` 下只允许 `requirements`、`blocked_output`、`guide` 三个键。出现后端名或 `approval_policy` 等调用参数时，直接拒绝。

### 能力表（`scripts/bf/environment.py` 的 `TABLE`，技能维护）

| kind | codex | claude | opencode | agy |
|---|---|---|---|---|
| host_parent | 驱动器与适配器检查父进程 NoNewPrivs（与后端无关） | 同左 | 同左 | 同左 |
| extra_dirs | `--add-dir`（沙箱可写根） | `--add-dir` | 无 OS 沙箱：native，标注“无隔离” | `--add-dir` |
| host_entry | 宿主 rules 的 allow 规则（`execpolicy check`），或同组合有 sandbox_escalation；均由模型冒烟证明 | unsupported（`--permission-prompts none` 下 Bash 会被拒，未验证） | 无 OS 沙箱：native | 未验证，unsupported |
| sandbox_escalation=auto_review | `approval_policy=on-request`（write 加 `--approve-for-me`，profile 核验） | unsupported | 无 OS 沙箱、无需提权：native | unsupported |

- 每格结果只能是 `mapped`（给出具体参数、规则或设置）、`native`（后端没有隔离，天然满足，在解析表中注明）或 `unsupported`。
- `unsupported` 不会被跳过：只要某个角色后端会**实施或审阅**适用的目标（该阶段轮次 > 0），就拒绝登记。负责人的协调调用（decide/triage）只获得其后端支持的需求，不因此拒绝；之后若追加轮次使其实施，调整同样经登记检查而被拒。
- 新增或修改映射，需要真实接入验收的证据。

### 解析与冒烟

- `bf env-resolve --plan P [--smoke]`：按角色（executor、backup_executor、lead、reviewer、third_party）、该角色的读写方式、它可能执行的目标，求出适用的需求集合，再查能力表得到参数，生成解析表并计算哈希。
- 冒烟按“后端 × 读写 × 需求集合”去重执行，回执写入 `<base>/records/env-smoke/<hash>.json`。冒烟方式：
  - **宿主检查**（所有组合）：host_parent 在冒烟进程中运行 `check_execution`；extra_dirs 检查目录存在且可写；无 OS 沙箱的后端在宿主直接运行 host_entry 的 `smoke`。
  - **一次模型调用**（有沙箱的后端，目前为 Codex，组合含 host_entry 或写模式 extra_dirs 时）：用解析后的真实参数发一张最小卡片，只执行 `smoke` 与目录写入/删除，再从 CLI 事件流确认每条命令确实执行且退出码为 0。宿主检查失败时不发模型调用。
    （实施时取消了原稿的 `codex sandbox` 无模型方案：它不加载 exec 流程的 rules 与审批，证明力不足。）
  - 通过的回执按组合键复用，组合键绑定需求内容、启动参数、profile 版本与宿主 rules 摘要。
- `init`、`revise`、`set-role` 都执行 `resolve`，并核对冒烟回执与当前解析哈希一致；缺回执、回执过期或冒烟失败都拒绝登记。冒烟只能经 `env-resolve --smoke` 运行，不消耗任何目标轮次。
- 计划中写入 `env_resolved = {hash, combos: {组合键: {backend, access, roles, requirements, launch, receipt, receipt_sha256}}, notes}`。驱动器在 `api.preflight` 中按工单重算组合键并比对，不重跑冒烟；不一致的工单暂停为 `environment_drift`（A1 完成后归属 owner=lead），其他工单照常派发，不消耗轮次。
- 离线测试套件以 `environment.ASSUME_SMOKE_FOR_TESTS` 模拟“冒烟已完成”；CLI 从不设置它，`test_bf_environment.py` 关闭它并验证真实回执路径。

### 卡片与适配器

- 卡片的“环境”一节由 `guide`、本单适用的需求项和解析结果生成，写明本角色实际得到的环境，例如“受控入口在宿主执行（codex 规则）”或“OpenCode 无隔离”。
- `adapter.argv` 只读取解析结果，不再读 `env_for`；`agent_cli` 的 `--approval-policy` 改为由解析器传入的内部参数，`validate_call` 保留作为最后一道防线。
- `gate.scan` 和 `evidence` 中受控入口的来源，改为 host_entry 需求的 `entries`。

### 迁移（旧计划转换）

`bf migrate-env --plan 旧 --out 新` 只负责转换和报告，不写状态，转换后仍须经 `revise` 登记：

| 旧字段 | 转换为 |
|---|---|
| `require_host` | host_parent |
| `add_dirs` | extra_dirs |
| `entry` + `preflight` | host_entry；`smoke` 需负责人补写，没有就报缺项 |
| `approval_policy=on-request` | sandbox_escalation=auto_review，`applies.access` 为 write |
| `blocked_output`、`guide` | 合并；各后端内容不一致时报差异，交负责人取舍 |

转换时的 `applies` 默认为 `"*"`，报告中提示负责人收窄。

### 项目侧：`run_board_test.sh selfcheck`（porting 仓库）

只做检查，不改任何状态：父进程 NoNewPrivs=0、`sudo -n -l` 精确端点、模拟器可执行文件及其 cap、record 工具可用、日志目录可写。不获取板测锁，不连接设备，不起模拟器，不碰网口。退出码 0 表示环境成立；失败时输出与 `blocked_output` 一致的特征。
这一项属于共享测试入口的修改，与测试重构任务共用目录，须在实施时核对对方的冻结窗口。

### 实施步骤

1. 新增 `capabilities.py`、`bf/envresolve.py`（resolve、smoke、hash），以及 `env-resolve`、`migrate-env` 两个子命令。
2. 修改 `model.normalize` 的 env 结构校验；`register.init/revise`、`adjust.set_role` 接入解析和回执核对；`adapter.argv`、`runner` 的宿主检查改读解析结果；同步修改 `cards`、`gate`、`evidence`。
3. 更新 references 文档：plan.md、design-book.md、cli-adapter.md、technical-lead.md 中的编制要求和样例。
4. 测试：离线覆盖解析、拒绝、回执、漂移和迁移；真实验收分别跑一次 codex 模型冒烟和一次 opencode 无模型冒烟。
5. `0-ethercat_reliability`：先在副本上用 `migrate-env` 转换，补写 selfcheck，执行 `env-resolve --smoke`，再经 `revise` 登记。切换技能需要你批准。

## 影响面与顺序

1. A3（能力表、解析器、env_resolved、冒烟；改动集中在 agent_cli、adapter.argv、model、register(init/revise/set-role)、cards 环境节、导入，以及 plan.md / design-book 的编制规范）。
2. A1（routing/policy/api/report 改为由 disposition 派生，加事务不变式；改动面最大，需要全量回归）。
3. A2（依赖 A1 的处置模型，在其上增加 hold ID、卡片状态节和 `waits_on` 重开）。

技能仓库当前有未提交改动（SKILL.md、api.py、cards.py、cycles.py、front.py 等）。实施前需要确认这些改动的归属，
并以其为基线或先行提交，避免交叉。实施期间不切换在用任务。切换前运行 `bf doctor`，并在副本上演练 `0-ethercat_reliability` 的导入。

## 不做

- 不为“technical_block + retry”单独加一条拒绝分支来打补丁，由 I-D 统一覆盖。
- 不在 init/revise 中扫描正文里的“当前/暂缓”等词来判错，只把它写入编制规范作为提示。
- 本提案不改前端提醒策略（D）和负责人 effort（E），按用户决定暂不处理。
