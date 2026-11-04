# 时间、轮次与返工追踪

## 保存与读取

本文的字段均指start/finish/resolve等操作的输入或metrics的只读输出；不授权编辑attempt、tracking或其他正式台账内容。字段可通过快捷命令的--data传入；复杂事务仍可经apply提交，无需另写追踪材料。

唯一正式来源仍是flow.json：attempt保存调用/交付，history保存操作与时间，state.tracking只保存状态变化。没有人工工时表、session表或第二份轮次日志。统计是派生结果，需要时生成：

```text
python3 <技能目录>/scripts/flow.py observe
python3 <技能目录>/scripts/flow.py metrics
```

均支持--store。status、validate、render、metrics只读；observe是明确的写操作，经锁、原子更新和history记录。无状态变化时observe不写文件、不递增revision。管理请求的每个操作前后自动观察，因此同一原子请求内的阶段变化也可保留；运行中和外部文件变化后由编排者调用observe再读取next。每次轮询不重复写同一状态。

tracking记录首次观察时间、任务状态/未满足依赖/未决裁决、批次完成或重新受阻、裁决处理阶段，以及脚本发现的待登记信号。不回填不存在的观察历史，没有后台自动观测保证。

## attempt：自动字段与交接字段

start自动保存task_snapshot（kind/role/scope/phase）、technical_round、开始版本、预算、计划版本和登记时间；所有新attempt同时保存performer_role与performer_config，作为本次调用责任及工具/模型/effort配置快照，finish补实际call按此快照核对。计划后续调整不改写已存调用归属。可选task.phase用于统计阶段命名，默认design、implementation；review直接依赖design时为design_review，否则implementation_review。计划/组审阅需技术负责人显式命名phase，如plan_review，避免混算。

编排者start可填call_id和triggers：

| start.triggers字段 | 输入含义 |
|---|---|
| attempts | 已结束的来源attempt ID数组，例如A0002 |
| decisions | 来源裁决ID数组，例如DEC-01 |

引用必须已存在，源attempt必须已经结束。脚本自动关联repair_tasks触发的修复、同任务失败后的续轮、复核前的修复，以及未闭环裁决要求的恢复任务。其他跨任务修订/裁决后的继续工作由编排者显式关联，不能只说“接上次”。纯首次工作triggers为空。INVALID的retry_of重试沿原技术轮次，调用次数仍单独增加。

finish可填call_id、timing和cause；已有生产字段仍兼容。调用计时由宿主/CLI外层实际捕获，不由角色估算：

| finish可选输入 | 含义 |
|---|---|
| timing.started_at / ended_at | CLI实际起止，带时区的ISO-8601时间 |
| timing.evidence | 固定CLI输出或调用元数据的非空路径数组 |
| cause.category | 下述受支持的原因分类，例如implementation_defect |
| cause.summary | 有依据的具体说明，关联finish.evidence |

以上描述finish操作的可选输入，不能直接写入store中的attempt；可通过finish --data传操作参数，复杂事务仍可交apply。timing必须在本次登记start/finish时间区间内，证据必须存在；未知不填，不用登记区间代替实际调用。生产禁止回填登记时间，但允许交付时补报有原始证据的实际调用时间。call_id是具体调用标识，conversation_id是可续用的会话标识，两者不能混用。工具内num_turns不作为技术轮次。

cause可选类别：implementation_defect、design_defect、missing_evidence、requirement_change、dependency_failure、tool_failure、other、unknown。角色只登记有依据的原因；缺陷性质由审阅角色判断，设计/范围根因由技术负责人诊断，编排者不猜测。后续诊断留在裁决原文，不覆盖既有attempt结论；统计中的cause表示当轮登记原因，不是最终责任归属。

## 耗时与轮次口径

- registration_seconds：登记start→登记finish，包括派发及结果入账等待。
- work_seconds：实际调用开始→实际调用结束；包含角色读文件、工具及构建等待，不叫纯模型思考时间。缺失为null，并报告unknown_work_calls。
- dispatch_seconds和result_registration_seconds：登记start→实际调用开始、实际调用结束→登记finish；均需实际计时。
- 各task的READY区间：首次观察到可执行→首次观察到状态变化。批次observed_ready_wait_seconds取所属任务READY区间的并集，避免并行等待重复计时；可能与其他任务工作重叠，不能与工作耗时相加当总历时。
- 批次elapsed_seconds：首次相关attempt开始→最近一次有效DONE观察；未完成算至查询时并标elapsed_open。旧记录已DONE但没有完成时间时返回null，不拿查询时间冒充完成时间。
- 阶段按phase分组。各节点分别展示技术轮次，D/DR/E/R属于不同任务，不把组内多个首审误称同节点R2；unknown历史不臆造模型或时间。
- rework_calls/rework_seconds统计非retry、非continuation且关联审阅/裁决来源的调用；含设计返修、实现修复和闭环复核。裁决后的原轮次剩余工作不自动算返工。可按phase进一步区分修改轮次与审阅轮次。未知耗时单独计数，不当成0。

投入记录不随预算分类删除：environment/registration免扣调用仍计入calls与已知work_seconds；metrics另列environment_calls、registration_calls、continuation_calls。attempt明细保留登记的round，并增加accounting_kind、chargeable、budget_root和continuation_of；technical_rounds及budgets.used按有计费工作的根轮次去重，同根任何后继有实质工作都保留一次占用。不同统计分组可能包含同一根轮次，不能把分组technical_rounds相加当全局总数。分类及续作通过[正式管理操作](protocol-budget.md#技术预算记账与续作)追加，不改写旧technical_round、结果、版本、计时及证据；没有分类的旧store沿用既有口径，不自动退还预算。

共享组评审在总计只出现一次；各批次exclusive只计本批独占调用，shared_attempts引用共享记录。阶段同样分独占统计与共享引用，不能把各批共享参与次数相加当实际总调用。并行调用的work_seconds之和可能大于总历时。

metrics同时返回attempt明细（节点/轮次/上下文/版本/实际计时/来源/证据）、批次阶段统计及task_timelines。若需要人读表，可从此JSON生成临时视图，不另维护正式表格。仅设计通道的必要设计内容也不手抄时间线，常规批次不建卡片。

## 每轮模型统计

metrics.attempts每条明细直接展示task、round、executor_stage、performer_role、tool、model、effort，并保留call、conversation_id（位于call内）、call_id和retry_of。模型取该次attempt.call，不取当前roles或当前handoff配置；计划后续改模型不改写历史。model_source=recorded_call表示来自该次登记调用，缺失为unknown且model=null，不用当前配置或推测填历史。

model_usage按实际职责、接管阶段、工具、模型、effort分组，列calls、technical_rounds、invalid_calls、retry_calls、已有耗时指标和attempt ID，直接关联原始每轮记录。主执行者、备选与负责人即使使用同一模型也按阶段区分；INVALID重试保留原技术轮次，单独增加调用次数。共享审阅只计一次，模型汇总仅覆盖attempt调用，架构诊断仍在decisions中单列，不混算。

编排者核对每次原生调用的实际工具/模型/effort和会话ID，再登记start/finish；宿主结果缺模型时保留实际调用参数作为依据，不把不明的自动模型替换当成计划模型。脚本能校验登记配置一致，不能认证远端实际推理模型或识别宿主未披露的切换；无需为统计再建人工模型台账。

## 裁决链

发现自动信号后，编排者按status的source_key登记signal。signals统计首次观察和未登记等待；已登记裁决关联同一source_key，计算detection_to_registration_seconds。无人登记的语义问题不可能从数据中推断出来。

派发技术诊断前以architect职责登记decision_start（id、call），resolve登记决定。审阅发现问题转对应技术链路的决策会话，不续审阅会话；同组后续裁决沿真实会话ID串行续接，只补差异，不虚构调用。无关组分开，跨组契约总体协调。兼任要求的context仍照常填写。decision_start若用pending会话ID，resolve.context补实际ID；已知ID不得被换成其他会话。

resolve可选timing、call_id，计时须在decision_start与resolve之间且有原始证据。裁决报告单列实际work_seconds，不混入attempt总耗时；历史上同一调用兼审阅/裁决的记录若引用同一call_id，只在一处填timing避免双算；新调用保持专用裁决会话与独立审阅分离。

记录报告、开始诊断、决定、恢复条件齐全、applied、closed及重新失效。registration_to_handling_seconds和handling_to_resolution_seconds是流程历时；observed_blocked_seconds统计未闭环观察区间，恢复后再次失效继续累计。各裁决阻塞可互相重叠，不能简单相加当批次总阻塞。

技术负责人未登记decision_start时，处理等待显示未知，不用resolve时间假设“即时处理”。预算耗尽仍先架构诊断，由技术负责人决定是否上升用户，追踪不改变裁决权限。

## 兼容与证据

format_version 仍为 1；tracking、task_snapshot、technical_round、triggers、performer_role/performer_config、timing/cause、call_id 均为可选新增字段，旧 store 可缺省。observe 必要时原子写观察，metrics 只读；decision_start 只允许 architect，不能代替 resolve，统计不豁免任何门禁。

旧store可直接读取；首次正式修改或observe才开始新观察，不自动迁移/重放历史。旧attempt无归属快照时以当前计划回退并标current_plan_fallback，其历史归属精度有限。模拟标志保留，模拟记录不能并入实际效率结论。时间倒序的旧模拟区间显示未知。

metrics只统计登记的数据，不重新认证每条技术结论。时间证据沿用原始调用记录，优先固定JSON/事件流已有时间字段；仅工具缺字段时补最小调用元数据。状态/统计命令不启动角色或执行产品验证；可选record仅捕获显式命令的实际结果，也不证明其他提交时间没有被伪造。

执行接管复用attempt记录：executor_stage区分primary/backup/lead，performer_role标实际责任。metrics保留计划任务role，并单列实际执行责任；换人不重置technical_round，INVALID沿retry_of复用该段技术轮次，不新增接管台账。

未实际派发的冗余启动可按预算协议明确分类 withdrawn：原 start 和 pending 配置保留，另记分类证据、source_attempt、withdrawn_at。它不计 calls、technical_rounds、unknown_work_calls 或返工，单列 withdrawn_starts；实际调用和真实技术消耗不受影响。客户端不能仅按原 attempt.result=null 把该占位当在途，使用 status 或 accounting_kind。
