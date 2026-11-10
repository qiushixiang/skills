# 计划编制（技术负责人）

计划是一份 `plan.json`。先与用户核对目标、范围、验收与依赖，写成计划草案交用户审阅；用户批准的原话保存为文件，再运行：

```text
python3 <技能>/scripts/bf/cli.py --base <任务>/.batch-flow init --plan plan.json --approval <用户原话文件>
```

修订用 `revise`，同样需要用户原话文件。已经用掉的额度不能被修订收回。

## 结构

```json
{
  "name": "任务名",
  "workspace": "/源码工作目录",
  "goal": {"objective": "项目目标", "constraints": ["…"], "non_goals": ["…"]},
  "roles": {
    "executor": {"tool": "codex", "model": "…", "effort": "medium"},
    "backup_executor": {"tool": "opencode", "model": "…", "effort": "default"},
    "lead": {"tool": "claude", "model": "…", "effort": "medium"},
    "reviewer": {"tool": "claude", "model": "…", "effort": "medium"},
    "third_party": {"tool": "codex", "model": "…", "effort": "medium"}
  },
  "policy": {
    "rounds": {"implement": 3, "backup": 1, "lead": 1, "repair": 2, "review": 7, "decide_per_source": 2, "invalid_retry": 2, "coordination_attempts": 2},
    "overrides": {"<交付目标>": {"implement": 4}},
    "context": {"max_session_kb": 300},
    "continue_limit": 2
  },
  "batches": [{"id": "S15", "title": "…"}],
  "deliverables": {
    "S15.E": {"batch": "S15", "title": "…", "instruction": "…", "acceptance": ["…"],
              "depends_on": ["S1314.R"], "resources": ["board"], "design_ref": "任务跟踪.md §3.16.5",
              "required_read": [{"path": "/任务/设计.md", "section": "§3.16.5 接口契约", "purpose": "实现接口时遵守已定语义"}],
              "constraints": ["仅完成本批范围"]}
  },
  "review_groups": {
    "S1516.R": {"deliverables": ["S15.E", "S16.E"], "reviewer": "reviewer"},
    "P3B.T": {"deliverables": ["…"], "batch": "S16", "reviewer": "third_party"}
  },
  "envelope": {"batches": ["S15", "S16"]},
  "env": {"codex": {"require_host": true, "add_dirs": [], "approval_policy": "on-request",
                    "entry": ["/…/run_board_test.sh"], "preflight": "/…/run_board_test.sh preflight",
                    "blocked_output": "Sandbox detected|Missing sudo rule", "guide": "…"}},
  "design": {"path": "/…/设计书.md", "sha256": "…"}
}
```

## 编制要点

- **设计书格式**：编制和修订均按[设计书](design-book.md)维护文首批次总表及对应详细章节。
- **交付目标**写“交付什么、怎样算通过”：instruction 写方案与范围边界，acceptance 逐条可核验。审阅、放行由脚本自动生成，不写进交付目标。
- **依赖**：`depends_on` 写交付目标或审阅组 ID；“本地提交”这类交付目标依赖它所提交内容的审阅组。
- **审阅节点**：仍由技术负责人在批次设计时安排，以 `review_groups` 表达，不必逐张编写审阅/返修工单。组定义检查范围、角色、重点和放行批次，运行时脚本生成首审、返修与重新审阅的工单。每个交付目标至少属于一个组；跨批次的阶段审阅写 `batch`（由该批放行等待）；第三方审阅写 `reviewer: third_party`。后续交付目标需要等审阅通过时，在 `depends_on` 引用该组 ID。
- **设计先审**：设计文档本身是独立交付目标，设计审阅组只覆盖该设计；实施目标的 `depends_on` 引用设计审阅组，产品审阅组覆盖实施交付。例如 `Sx.D → Sx.DR → Sx.E → Sx.R`。不能复用覆盖 `Sx.E` 的产品审阅组作前置设计审阅，也不能只在 instruction 写“先审后做”。新增实际设计工作要明确预算来源，不能靠换目标重置原实施额度。
- **资源**：会互相干扰的交付目标（如共用一块板）写相同的 `resources`，并发运行时不会同时派发。
- **授权包络**：`envelope.batches` 是用户批准可自主推进的批次；可选 `envelope.protected: {"目标": ["精确仓库相对路径"]}` 预授限定路径权限。预授权只覆盖指定目标和路径，仍遵守设计中的修改内容、验收和审阅要求。包络外的工作由脚本转交用户。
- **派发需求**：已知实施需要改保护路径时，在交付目标的 `required_protected` 列出精确路径，驱动器在派发前检查。它声明需求，不授予权限；不要把已知需求只藏在指令文字里。
- **环境**：需要宿主权限的验证只经 `env.entry` 受控入口；`blocked_output` 写入口自身的环境失败输出，作为环境诊断事实，由角色判断是否阻塞。
- 编制时必须配套审阅预算，不能依赖默认额度或运行中再追加。单目标保守预留 `max(1, implement) + repair + backup + lead`；未配置备用角色时不计 backup。合并审阅组按 `1 + Σ(各目标预留 - 1)` 预留，联合首审只计一次，各目标返修机会分别覆盖；第三方审阅组独立配额。所有值使用目标/审阅组 override 后的实际额度。

## 修订、额度与版本

- 实施额度由同一交付目标的全部实施单共享；返修额度由同一审阅周期与返修目标共享；审阅额度由同一审阅组共享。换单、换上下文和修订计划不重置用量。追加审阅额度用 `grant <审阅组> --review N --approval F`。
- `contract_version` 记录契约版本。文字或引用变化不自动重做工作；负责人用 `revalidate` 明确选择需重做目标，其余历史结论保留，由角色判断适用性。
- 有受影响工单正在运行时先停止派发并完成回收，再修订；脚本拒绝混用运行中的旧输入与新契约。新契约仍沿用已消耗的额度，必要时在本次批准中追加。
- 后续实施与返修统一使覆盖该目标的审阅等待当前工作，已通过的常规/第三方审阅需重新确认；放行等待相应工单完成。

## 完整性字段（design §24）

- `repos`：源码仓库绝对路径。启用版本观察与自动快照（包括失败/中断），保存 SHA 和类型/模式等事实；不因检测到变化自动判交付失败。
- `protected`：负责人明确的保护边界提示，不把整个任务内文件一律冻结。实际变化交角色判断；显式 required_protected 权限需求仍须正式登记，不能绕过宿主或用户明确授权。
- `no_review`：交付目标不属于任何审阅组时必须写明原因，否则计划不通过。
- `design_ref`：设计章节引用。保存章节哈希，变化作为事实送达角色，不自动暂停实施；负责人判断影响与需要重做的目标。
- 可选 `deliverables.*.non_goals`（字符串列表，卡片目标节列出“不要做”）与 `review_groups.*.focus`（字符串，审阅卡列出审阅重点）。计划编制方法见 [技术负责人](technical-lead.md)。
- `instruction`、`acceptance` 不得为空。完整路径审阅额度不足时登记失败，不再仅写 `plan.warnings`。修订/增额不得新增或扩大缺口；新建、重做或改变成员的审阅路径必须全额覆盖。历史不变缺口保留原批准与用量，派发前仍受硬门禁约束，不自动追加额度。

## 协调与额度

`lead` 为兜底实施轮次。decide/triage 使用独立 coordination_attempts（默认每单 2 次）。decide_per_source（默认 2 次）及历史字段 coordination_no_progress（默认 6 次）现仅表示协调调用次数上限，不推断技术进展；额度与权限变化不自动重置次数，运维处置后显式 retry/escalate 开放有界恢复。

达到协调上限只暂停受影响路径并报告，不自动询问用户。[六类新增用户决定](user-decisions.md)之外的技术问题由负责人和运维处理。纯技术方案调整可由负责人声明 plan_revision.within_authorization，脚本检查目标、验收、授权、预算、资源、依赖和必需审阅不变后执行；真正改变用户承诺的计划修订仍须批准。

技术调用按 performer 记录；负责人结算 0/1 后计入对应轮次，未结算先占位，不自动爬升执行者。只有明确整次未启动可机械免扣；其余由负责人判断。协调单独记账，历史原记录保留。

## 修订影响

负责人判断修订是否改变用户承诺；within_authorization 仅表示负责人的裁定，结构比较只保护显式预算、权限和流程配置，不证明自然语言等价。新增承诺仍须用户批准。
指令、验收或设计措辞变化不自动使已交付内容失效。plan_revision 的 revalidate（或 revise --revalidate）明确列出需重做的现有交付目标；覆盖这些目标的独立审阅按现有依赖执行，其余下游不自动重开。负责人必须判断并列全受影响目标。

## 上下文与必读

`policy.context.max_session_kb` 是实施续轮的输出日志大小阈值，不是模型上下文窗口。复审续用原审阅会话，负责人技术裁决与纯预算结算按同一交付目标续用会话；二者不应用该阈值。

负责人在设计阶段按[工单上下文编制](technical-lead.md#工单上下文编制)选定必需输入。目标及审阅组可配置 `required_read: [{"path": "...", "section": "准确范围", "purpose": "具体用途", "sha256": "可选SHA256"}]`；专项单 `input.required_read` 替换该角色默认清单。目标 `constraints` 为当前有效要求，不保存历史覆盖语句。

目标和验收以计划为唯一来源；专项 `input.instruction` 表达本次差异。`execution_notes` 仅保留历史审计，不再自动送给执行者；仍有效的必要规范须整理到正文或必读。卡片和 dispatch.json 只保存实际派发导出，不是可独立修改的任务来源。脚本只核对结构和导出保真；选材、约束含义与版本影响由负责人/审阅者判断。


执行控制、预算结算和换人原则见 [技术负责人](technical-lead.md#预算结算)。历史协议保留，不自动迁移任务。

### 审阅预算配套脚本门禁

`bf/review_budget.py` 统一计算计划覆盖和派发保留。实施/返修派发前按同一预算账本扣除已用及待结算技术机会，保留剩余独立审阅（至少一次）；审阅使用量按组累计，修订/换单不重置，免扣/冲正按账本处理。缺口由 preflight 记录为 `review_budget_shortfall`，不调用执行角色；直接 start 同样拒绝。追加实施/返修额度须先配足审阅，或在同一批准动作包内同时追加；历史计划不会被静默改额度。
