# 分批计划初始化示例

技术负责人在用户审阅通过设计书后，用本例为模板提交 `init`。本例已对照 `scripts/flow.py` 实际跑通 init → authorize → validate → status（B1.D 进入 READY）。字段含义见[协议](protocol-plan.md#计划结构)；本文只给可直接改写的最小形状，不重复规则。

示例含两批：B1 走设计通道（批次卡 D → 卡审 DR → 实现 E → 审阅 R），B2 走常规通道（E → R）；每批一个放行节点，B2 依赖 B1 放行。

```json
{
  "plan": {
    "name": "示例任务",
    "roles": {
      "architect": {"tool": "claude-code", "model": "<用户指定>", "effort": "<用户指定>", "principal": "lead"},
      "reviewer": {"tool": "claude-code", "model": "<用户指定>", "effort": "<用户指定>", "principal": "lead"},
      "executor": {"tool": "claude-code", "model": "<用户指定>", "effort": "<用户指定>"},
      "orchestrator": {"tool": "claude-code", "model": "<用户指定>", "effort": "<用户指定>"}
    },
    "batches": {
      "B1": {"name": "生命周期硬化", "completion_task": "B1.release"},
      "B2": {"name": "周期交换", "completion_task": "B2.release"}
    },
    "subjects": {"B1.design": "d0", "B1.code": "c0", "B2.code": "c0"},
    "budgets": {
      "B1.D": {"limit": 2}, "B1.DR": {"limit": 4}, "B1.E": {"limit": 3}, "B1.R": {"limit": 5},
      "B2.E": {"limit": 3}, "B2.R": {"limit": 5},
      "backup.B1": {"limit": 1}, "lead.B1": {"limit": 1},
      "backup.B2": {"limit": 1}, "lead.B2": {"limit": 1}
    },
    "tasks": {
      "B1.D": {"kind": "design", "role": "executor", "scope": ["B1"], "subjects": ["B1.design"], "depends_on": [], "consumes": ["B1.D"], "session_policy": "resume",
        "handoff": {"backup": {"tool": "claude-code", "model": "<用户指定>", "effort": "<用户指定>"}, "backup_budget": "backup.B1", "lead_budget": "lead.B1"},
        "instruction": "本批目标：……。本批方案：写批次卡，覆盖未决问题与验证依据。完成标志：……。设计依据：design.md，第1批：生命周期硬化，计划版本1。"},
      "B1.DR": {"kind": "review", "role": "reviewer", "scope": ["B1"], "subjects": ["B1.design"], "depends_on": ["B1.D"], "consumes": ["B1.DR"], "repair_tasks": ["B1.D"], "session_policy": "resume",
        "instruction": "审阅批次卡能否支撑本批目标；通过后才可实现。"},
      "B1.E": {"kind": "execute", "role": "executor", "scope": ["B1"], "subjects": ["B1.design", "B1.code"], "depends_on": ["B1.DR"], "consumes": ["B1.E"], "session_policy": "resume",
        "handoff": {"backup": {"tool": "claude-code", "model": "<用户指定>", "effort": "<用户指定>"}, "backup_budget": "backup.B1", "lead_budget": "lead.B1"},
        "instruction": "本批目标：……。本批方案：按已审批次卡实现。完成标志：……。设计依据：design.md，第1批：生命周期硬化，计划版本1。"},
      "B1.R": {"kind": "review", "role": "reviewer", "scope": ["B1"], "subjects": ["B1.code"], "depends_on": ["B1.E"], "consumes": ["B1.R"], "repair_tasks": ["B1.E"], "session_policy": "resume",
        "instruction": "独立审阅实现并复跑关键验证。"},
      "B1.release": {"kind": "acceptance", "role": "orchestrator", "scope": ["B1"], "subjects": ["B1.code"], "depends_on": ["B1.R"], "consumes": [],
        "instruction": "机械核对门禁与证据齐全后放行。"},
      "B2.E": {"kind": "execute", "role": "executor", "scope": ["B2"], "subjects": ["B2.code"], "depends_on": ["B1.release"], "consumes": ["B2.E"], "session_policy": "resume",
        "handoff": {"backup": {"tool": "claude-code", "model": "<用户指定>", "effort": "<用户指定>"}, "backup_budget": "backup.B2", "lead_budget": "lead.B2"},
        "instruction": "本批目标：……。本批方案：……。完成标志：……。设计依据：design.md，第2批：周期交换，计划版本1。"},
      "B2.R": {"kind": "review", "role": "reviewer", "scope": ["B2"], "subjects": ["B2.code"], "depends_on": ["B2.E"], "consumes": ["B2.R"], "repair_tasks": ["B2.E"], "session_policy": "resume",
        "instruction": "独立审阅实现并复跑关键验证。"},
      "B2.release": {"kind": "acceptance", "role": "orchestrator", "scope": ["B2"], "subjects": ["B2.code"], "depends_on": ["B2.R"], "consumes": [],
        "instruction": "机械核对门禁与证据齐全后放行。"}
    },
    "decision_points": {},
    "context_groups": {"core": ["B1", "B2"]}
  },
  "simulation": false,
  "task_mode": "batched",
  "context": {"conversation_id": "<规划会话真实ID>", "mode": "fresh", "reason": "用户审阅通过后的初次登记"},
  "design": {"path": "../design.md", "version": "v1", "plan_version": 1,
    "sections": {"B1.D": "第1批：生命周期硬化", "B1.E": "第1批：生命周期硬化", "B2.E": "第2批：周期交换"}}
}
```

## 容易出错的地方

- `roles` 只有 `architect` 必填；用户授权先落盘计划时，其他角色和 `handoff.backup` 可先省略，相关任务显示 WAIT_MODELS，之后由编排者经 set_models 首次登记（须给全 tool/model/effort）。`<用户指定>` 处填用户为本任务指定的值，不自行推定。
- `design.path` 相对 store 所在目录（`.batch-flow/`）解析，任务根下的设计书写 `../<文件>`，或用绝对路径。
- `design.sections` 必须覆盖全部 design/execute 任务，且章节名原文同时出现在设计书和该任务 instruction 中。
- `handoff` 只能挂在 executor 的 design/execute 任务上；备选预算 limit 必须为 1，备选与兜底预算互不相同，也不能被任何任务 `consumes`。同批 D 与 E 复用同一组备选/兜底预算，共用一次备选机会。
- 审阅配额覆盖完整接管路径：D 主2＋备用1＋兜底1对应 DR4；E 主3＋备用1＋兜底1对应 R5。共享备用机会实际只消耗一次，按各独立节点的保守上界预留；init/plan_update 不接受审阅额度不足的模板。
- 放行节点（acceptance）由编排者机械核对门禁；技术判断在审阅节点完成，审阅会复跑关键验证。

## 提交与登记

```text
mkdir -p <任务根>/.batch-flow
python3 <技能目录>/scripts/flow.py init --store <任务根>/.batch-flow/flow.json --revision 0 --actor architect --data @init.json
python3 <技能目录>/scripts/flow.py authorize --store <任务根>/.batch-flow/flow.json --revision 1 --actor orchestrator --data '{"batches":["B1","B2"],"evidence":["<用户审阅通过原话文件>"]}'
```

init 由技术负责人在用户审阅通过后提交；authorize 由编排者登记，证据引用技术负责人保存的用户审阅通过原话（见[计划编制](../workflows/plan-sync/SKILL.md)）。
