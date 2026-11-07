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
    "rounds": {"implement": 3, "backup": 1, "lead": 1, "repair": 2, "review": 3, "decide_per_source": 2, "invalid_retry": 2},
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
- **审阅组**：每个交付目标至少属于一个组；跨批次的阶段审阅写 `batch`（由该批放行等待）；第三方审阅写 `reviewer: third_party`。
- **资源**：会互相干扰的交付目标（如共用一块板）写相同的 `resources`，并发运行时不会同时派发。
- **授权包络**：`envelope.batches` 是用户批准可自主推进的批次；包络外的工作由脚本转交用户。
- **环境**：需要宿主权限的验证只经 `env.entry` 受控入口；`blocked_output` 写入口自身的环境失败输出，用于机械判定“外部阻塞”。
- 默认额度通常够用；个别交付目标需要更多轮次时写 `overrides`，并在审阅计划时向用户说明。
