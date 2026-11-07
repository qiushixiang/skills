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
    "rounds": {"implement": 3, "backup": 1, "lead": 1, "repair": 2, "review": 3, "decide_per_source": 2, "invalid_retry": 2, "coordination_attempts": 2},
    "overrides": {"<交付目标>": {"implement": 4}},
    "context": {"max_session_kb": 300},
    "continue_limit": 2
  },
  "batches": [{"id": "S15", "title": "…"}],
  "deliverables": {
    "S15.E": {"batch": "S15", "title": "…", "instruction": "…", "acceptance": ["…"],
              "depends_on": ["S1314.R"], "resources": ["board"], "design_ref": "任务跟踪.md §3.16.5"}
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

- **交付目标**写“交付什么、怎样算通过”：instruction 写方案与范围边界，acceptance 逐条可核验。审阅、放行由脚本自动生成，不写进交付目标。
- **依赖**：`depends_on` 写交付目标或审阅组 ID；“本地提交”这类交付目标依赖它所提交内容的审阅组。
- **审阅节点**：仍由技术负责人在批次设计时安排，以 `review_groups` 表达，不必逐张编写审阅/返修工单。组定义检查范围、角色、重点和放行批次，运行时脚本生成首审、返修与重新审阅的工单。每个交付目标至少属于一个组；跨批次的阶段审阅写 `batch`（由该批放行等待）；第三方审阅写 `reviewer: third_party`。后续交付目标需要等审阅通过时，在 `depends_on` 引用该组 ID。
- **设计先审**：设计文档本身是独立交付目标，设计审阅组只覆盖该设计；实施目标的 `depends_on` 引用设计审阅组，产品审阅组覆盖实施交付。例如 `Sx.D → Sx.DR → Sx.E → Sx.R`。不能复用覆盖 `Sx.E` 的产品审阅组作前置设计审阅，也不能只在 instruction 写“先审后做”。新增实际设计工作要明确预算来源，不能靠换目标重置原实施额度。
- **资源**：会互相干扰的交付目标（如共用一块板）写相同的 `resources`，并发运行时不会同时派发。
- **授权包络**：`envelope.batches` 是用户批准可自主推进的批次；可选 `envelope.protected: {"目标": ["精确仓库相对路径"]}` 预授限定路径权限。预授权只覆盖指定目标和路径，仍遵守设计中的修改内容、验收和审阅要求。包络外的工作由脚本转交用户。
- **派发需求**：已知实施需要改保护路径时，在交付目标的 `required_protected` 列出精确路径，驱动器在派发前检查。它声明需求，不授予权限；不要把已知需求只藏在指令文字里。
- **环境**：需要宿主权限的验证只经 `env.entry` 受控入口；`blocked_output` 写入口自身的环境失败输出，用于机械判定“外部阻塞”。
- 默认额度通常够用；个别交付目标需要更多轮次时写 `overrides`，并在审阅计划时向用户说明。

## 修订、额度与版本

- 实施额度由同一交付目标的全部实施单共享；返修额度由同一审阅周期与返修目标共享；审阅额度由同一审阅组共享。换单、换上下文和修订计划不重置用量。追加审阅额度用 `grant <审阅组> --review N --approval F`。
- `contract_version` 由脚本维护。指令、验收、范围、依赖或引用设计章节实质变更时，`revise` 为受影响目标及其依赖方创建新契约下的实施与审阅；旧交付保留，不能证明新契约完成。审阅组成员、角色或重点改变也需要重新审阅。只调额度、模型等不会重做实施。
- 有受影响工单正在运行时先停止派发并完成回收，再修订；脚本拒绝混用运行中的旧输入与新契约。新契约仍沿用已消耗的额度，必要时在本次批准中追加。
- 后续实施与返修统一使覆盖该目标的审阅等待当前工作，已通过的常规/第三方审阅需重新确认；放行等待相应工单完成。

## 完整性字段（design §24）

- `repos`：源码仓库绝对路径列表。写了才启用自动快照：每次运行前记录脏文件基线，登记时算出本次改动并生成快照；审阅卡因此可写源码仓库之外的 records（审阅访问为 write，但改动源码会被判 `review_modified`）。不写则不做快照。
- `protected`：受保护路径 glob（如 `tests/run_board_test.sh`）。未授权改动登记为 FAILED 并转技术负责人（trigger `protected_change`）。给原单补权限使用裁决 `authorize`；新单可带 `allow_protected`。超出计划预授权的部分才转用户；批准后须结构化登记，不能以批准原文代替权限状态。
- `no_review`：交付目标不属于任何审阅组时必须写明原因，否则计划不通过。
- `design_ref`：设计文档章节标题（有 `design.path` 时每个交付目标必填，须能匹配标题）。脚本记下章节哈希（`design.sections`）；章节后来被改，相关交付目标暂停派发，并开 `design_drift` 裁决（处置 `{"refresh_design": true}` 表示方案不变，重新固定哈希）。
- 可选 `deliverables.*.non_goals`（字符串列表，卡片目标节列出“不要做”）与 `review_groups.*.focus`（字符串，审阅卡列出审阅重点）。计划编制方法见 [技术负责人](technical-lead.md)。
- `instruction`、`acceptance` 不得为空。`policy.rounds.review < 1 + 返修链长度` 时写入 `plan.warnings`，审阅轮次可能不够。

## 协调与额度

`lead` 仅为兜底实施轮次。decide/triage 使用独立的 `coordination_attempts`（默认每单 2 次），不计技术轮次。`decide_per_source` 限制同类问题在相同相关状态下重复裁决（默认 2 次），不是某目标的历史裁决总数。待裁决可用“问题标识”区分稳定的不同阻塞；目标同时受 `coordination_no_progress` 总无进展上限（默认 6 次）约束，不能靠改标识绕过。有效权限、预算、契约或本目标源码变更属于进展；换工单、改描述、普通只读记录和 retry 不重置计数。

达到协调上限只暂停受影响路径并报告，不自动询问用户。[六类新增用户决定](user-decisions.md)之外的技术问题由负责人和运维处理。纯技术方案调整可由负责人声明 plan_revision.within_authorization，脚本检查目标、验收、授权、预算、资源、依赖和必需审阅不变后执行；真正改变用户承诺的计划修订仍须批准。

实施运行只有在工具事件能证明全程为受支持的只读诊断、无编辑或源码变化、无受控入口调用，并以待裁决结束时才免扣。无变化本身不构成免扣依据，未知脚本与真实构建测试仍计技术轮次。历史误记只经批准后的审计冲正，不能删除或改写运行。
