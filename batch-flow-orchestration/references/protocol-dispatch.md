# 操作清单、派发登记与查询

全部管理操作的责任角色与必填字段、快捷命令和 status/pre_yield 输出。字段总览与存储规则见[协议总览](protocol.md)。

## 请求与操作

底层请求顶层字段如下；快捷命令自动生成信封，复杂事务才由角色构造并交apply，不写入正式台账路径：

| 字段 | 含义 |
|---|---|
| request_id | 本次唯一请求ID；重复同内容幂等 |
| expected_revision | 本次读取的revision；首次init为0 |
| actor | 实际责任职责 |
| operations | 非空操作数组；每项含type及该操作的data |
| at | 仅显式模拟允许；正式操作时间由程序生成 |

例如首次规划提交actor=architect、type=init，data提供完整plan、simulation、context、task_mode（分批还须design）；程序生成state、decisions、history，不要求角色手工构造这些区。

actor只能是五类角色；用户决定由编排者附原始来源登记。仅模拟允许回填历史started_at/ended_at。

| 操作 | 责任角色 | data 必填字段；可选字段用括号 |
|---|---|---|
| init | 技术负责人 | plan, simulation, context, task_mode；local 可省略 design，batched 必须有 design |
| plan_update | 技术负责人 | plan, decision_id，(context, design)；已有设计关联时两者必填，保留原有ID；预算限额变更、新增池、替换既有预算绑定需用户决定；审阅预算配套强制校验 |
| design_refresh | 技术负责人 | version, reason, context；仅受控纯文字纠错，更新文档哈希而不增加 plan_version |
| set_models | 编排者 | roles, backups, reason, user_evidence；用户明确指定的tool/model/effort局部更新，直接登记完成，不要求架构裁决 |
| set_user_mode | 编排者 | mode, user_evidence；(authorization，away时必填)；登记/解除用户离开临时委托，不改变原计划、预算或门禁 |
| register_dispatch | 当前dispatch_owner | attempt, handle, evidence；原生计时另给 timer_kind=native、timer_id、owner_conversation_id、due_at；脚本计时给 timer_handle、timer_ticket。仅关联真实回执 |
| authorize | 编排者或局部技术负责人 | batches, evidence；全局编排者替换授权范围，技术负责人只能在完全委派的批次内追加且保留其他授权 |
| start | 当前dispatch_owner | task, call；(retry_of, continuation_of, replacement_reason, started_at, triggers, call_id)；续作使用status给出的continuation_of，不能与retry_of并用 |
| finish | 任务责任角色 | attempt, result, evidence；(closes, ended_at, call, subject_versions, timing, cause, call_id) |
| change_subject | 执行者/技术负责人（执行者职责由dispatch_owner代交） | subject, version, evidence |
| signal | 任一角色（执行者/审阅职责由dispatch_owner代交） | id, signal, reason, blocks, evidence；(decision_point, source_key) |
| decision_start | 技术负责人 | id, call；登记待决事项开始诊断，不改变决定内容；仅修复旧无效 Codex 逻辑会话时可附 replacement_reason、evidence，保留 handling_history，见 contexts.md |
| resolve | 技术负责人 | id, summary, requirements, evidence, user_evidence；(context, timing, call_id, clarification) |
| repin_requirement | 技术负责人 | id, requirement, versions, evidence；(context, reason，二者至少一项)；仅重钉已有 task 恢复条件到目标当前完整版本映射 |
| classify_attempt | 技术负责人 | attempt, kind, work_performed, reason, evidence；(source_attempt, decision_id, context)；追加可审计记账分类，不改写原尝试 |
| resume_attempt | 技术负责人 | attempt, reason, evidence；(context)；依据实际恢复证据有限批准最新environment/decision_pause尝试续作 |
| decision_progress | 编排者或负责该局部范围的技术负责人 | id, status, evidence；(requirement) |
| feedback | 任一角色 | attempt, summary, evidence |
| observe | 编排者或局部技术负责人 | 空对象；仅观察并记录状态变化，亦可用observe命令 |
| history | 编排者，仅模拟 | task, call, result, evidence, source；(started_at, ended_at, closes, replacement_reason) |

init.data 必须给 plan、simulation、context 和 task_mode（local 或 batched）；batched 必须关联设计，local 可无设计。旧台账仍可读取且不自动迁移，不接受旧格式新建。local 增加批次时必须同时关联设计。合并负责人后续plan_update/resolve继续强制context。编排者可以纠正派发模板格式，但技术负责人已返回的错误载荷应退回修正，不为绕过校验修改脚本或擅删角色数据。

编排者的actor=orchestrator不能执行init、plan_update、resolve或change_subject，也不能finish其他角色的任务。代交时actor仍是原责任角色且技术载荷不变，不等于编排者获得该职责。actor是责任标签，脚本不能认证谁实际提交或限制shell/文件访问；职责隔离由派发约束与宿主权限共同落实，不宣称角色校验可以阻止所有文件越权。

call 包含 tool/model/effort/conversation_id；handoff任务按status的call_config核对。start 时会话未知可暂用 pending:xxx，finish.call 必须补实际记录；技术成功不能接受未确认会话。角色模型配置由工具调用原始结果核实，脚本检查登记值一致，不能认证远端实际使用模型。

执行者在实现过程中更新对象版本后，可在 finish.subject_versions 显式声明当前完整版本映射；程序另存 started_subjects，保留开始版本。审阅角色不能借此更换被审版本。

所有 evidence 是非空路径列表，绝对路径或相对 store 所在目录。写入时解析为绝对路径并计算SHA256；结果参与放行前检查文件仍存在且内容未变。原始报告使用不可变路径，派生视图不要充作自身证据。

result 允许 VERIFIED/RELEASE/FIX_REQUIRED/WAIT_EVIDENCE/ARCH_DECISION_REQUIRED/FAILED/INVALID。完成结果不可覆盖，改正通过新尝试及关联证据表达。预算在 start 时预占，INVALID重试不增加技术轮次，但原始调用仍留在 attempts 中。

## 快捷命令

以下<REV>来自刚核对的status，不自动取最新值或重试写入。--note仅存一次必要交付/技术意见，不能冒充命令输出。--evidence可重复，路径规则与apply相同。

```text
python3 <技能目录>/scripts/flow.py start --revision <REV> --task B01.E --tool <实际工具> --model <实际模型> --effort <实际effort> --conversation <会话ID>
python3 <技能目录>/scripts/flow.py record --cwd <源码目录> -- <验证程序> <参数>
python3 <技能目录>/scripts/flow.py snapshot --repo <源码仓库> --paths <本批文件或目录>
python3 <技能目录>/scripts/flow.py finish --revision <REV> --actor executor --attempt <本次attempt> --result VERIFIED --evidence <原始验证文件> --evidence <必要差异文件> --note <AC结果与限制>
python3 <技能目录>/scripts/flow.py signal --revision <REV> --actor executor --id DEC-01 --signal contract_change --reason <矛盾与影响> --blocks <受阻任务ID> --evidence <直接依据> --note <可行处理及建议>
python3 <技能目录>/scripts/flow.py resolve --revision <REV> --id DEC-01 --summary <澄清结论> --clarification --conversation <架构会话ID> --note <依据及无需后续动作的理由>
python3 <技能目录>/scripts/flow.py close --revision <REV> --id DEC-01 --evidence <恢复结果依据>
```

resolve普通恢复、finish.subject_versions/call、init、plan_update、authorize、change_subject、feedback、decision_start、decision_progress等使用--data传本操作参数（JSON、@文件或-从stdin读），--actor指定实际职责。无需request_id/expected_revision/operations信封；仍须--revision和实际参数。--data与同名快捷参数不得重复；evidence可以追加。start的实际call通过必需的四个快捷参数提供，脚本不把计划模型冒充实际调用配置。

```text
python3 <技能目录>/scripts/flow.py resolve --revision <REV> --conversation <架构会话ID> --data -
python3 <技能目录>/scripts/flow.py change_subject --revision <REV> --actor executor --data @<操作参数文件>
```

--context-mode resume配合--conversation自动记录同一来源ID。默认start/close为orchestrator，resolve为architect；其他操作显式--actor。普通resolve的--data仍需给requirements、summary、id和evidence，user_evidence默认[]。有跨actor操作时分开登记，不能用快捷入口绕过职责。

record、snapshot、`--repo/--git-ref` 的行为与证据路径规则见[证据记录与版本](engineering.md#证据记录与版本)。

快捷命令复用apply，仍有职责/版本/CAS检查。revision冲突必须重新核查，不能盲升revision。apply保留原有请求幂等语义；--note/--git-ref每次生成新记录，快捷命令失败后不要假设原命令可原样幂等重发，应读取状态确认是否已生效。

## 输出与限制

AGY 启动器会话中，CLI status/pre_yield 和写操作的 next 返回 host_capabilities，以及派发动作 cli_adapter.profile。只读 `capabilities --store <绑定台账>` 可单独恢复同一信息，不依赖台账已初始化；不搜索 runtime，不读取其他启动会话。该宿主元数据是输出派生信息，不写入正式 plan/flow.json，详细绑定与权限见 hosts-agy-codex.md。

decision_progress 的 confirmed 可带 budget_approval，把后续用户批准的预算范围与原始证据一次保存在 confirmation 中；bind_budget_approval 仅给已有未标注的用户确认补范围，不回填裁决头。plan_update 统一核验两种授权来源、证据有效性与作用范围，详见[先建议、后批准](protocol-decisions.md#先建议后用户批准单处登记)。

status 另给 review_budget_coverage（各审阅来源、保守所需轮次、各预算池需求和缺口）。validate 对配套不足返回 ok=false、退出码2；status/metrics 仍可读取旧台账。相关实施/返修派发遇到配套不足或审阅额度耗尽时为 WAIT_REVIEW_BUDGET，给技术负责人 PLAN_REPAIR，start 拒绝消费执行额度。规则与历史完成任务处理见[审阅预算配套](protocol-budget.md#审阅预算配套脚本门禁)。

status 给出 revision/plan_version/simulation/batches/tasks/actions/complete。actions 中包含角色、目标、版本、剩余预算等。每条动作给出顶层 `orchestrator_todo` 和 `orchestrator_action.mode/instruction`：`dispatch` 真实派发，`delegate_to_lead` 移交局部负责人，`collect_progress` 收集进度，`register` 协调登记，`request_user` 请求用户决定。派发/移交动作的技术 `instruction` 已从顶层移入 `subagent_payload`，连同角色、调用配置、版本和上下文交给责任人；旧消费者须改读该字段，不能继续读取 action.instruction。正式 plan.tasks 的 instruction 和台账格式保持不变。技术任务若误设 role=orchestrator，状态为 WAIT_ROLE_ASSIGNMENT，先派技术负责人修订归属，不生成编排者直接执行动作。`role` 与 `actor` 仍不认证实际执行身份。REGISTER_DECISION 带 source_key，可登记为 signal，防止重复挂同一问题。查询不会自动写裁决。

`pre_yield`只读检查当前责任范围能否交还控制权，默认全局，局部负责人用 `--actor architect`；旧记录若运行 attempt 的派发负责人和当前计划不一致，按 attempt 原责任人继续登记回执及检查，先将计划改回原负责人或等结束后交接，不改写历史。运行中的负责人不得变更，包括从显式 architect 改为默认 orchestrator。CONTINUE/STALLED退出1，WAIT_HOST/WAIT_USER/COMPLETE退出0。它检查运行attempt的真实dispatch回执、原生定时到期或脚本票据状态及证据漂移；不会仅凭RUNNING/pending认定后台运行。register_dispatch接受未结束attempt、真实handle、未到期的原生schedule回执或脚本活动ticket，以及非空不可变宿主回执；更新计时不能替换工作handle。详情见[实际派发](agent-collaboration.md#实际派发与交还控制权)。脚本不证明进程存活，宿主真正接入stop hook才可拦截退出；技能不自行安装hook。

REPORT_PROGRESS 提示登记进度，不等于自动升级架构或停止。无后台守护进程，不承诺模型不调用脚本时仍能主动提醒。状态脚本不检查源码技术正确性、文件写入冲突或实际工具权限；这些由角色和宿主控制。

可通过 `pre_yield --conversation <编排者ID> --audit-log <日志>` 启用[角色审计](role-audit.md)，额外管理目录用 `--audit-management-root`。越权或不可审计返回 ROLE_BOUNDARY_BLOCKED/退出1，即使任务已完成也不放行；未配置时 boundary_audit.status=not_configured，不能当作已审计。审计采集、会话范围、用户停止优先及宿主接入见对应文档。
