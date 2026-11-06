# 计划、设计关联与模型登记

init、plan_update、design_refresh 与 set_models 的参数规则；初始化形状见[初始化示例](init-example.md)。字段总览与存储规则见[协议总览](protocol.md)。

## 计划结构

分批规划同时交付[Markdown设计书](artifacts.md#架构规划必须交付人类可读设计书)，供用户评审方案；这不自动增加独立技术审阅。新任务 init 必须同时提交 plan、simulation、作者 context 和 task_mode；local 可无 design，batched 必须关联 design。旧台账可读取，不接受旧格式新建。design 是 path、version、plan_version、sections（任务ID到章节名），不把正文塞进台账。实质变更 plan_update 同时提交新 plan、context 与 design；脚本校验文件、章节、哈希、版本和指令引用，但不能判断自然语言语义一致。

**哈希只覆盖被引用的章节。** 章节名等于某个 Markdown 标题时，范围是该标题到下一个同级或更高级标题；否则是包含该名称的段落（前后空行之间）。设计书其他部分（如决议记录、未闭环事项）可随时修改，不触发同步门禁；某章节变化或消失时，只有映射到它的任务及其下游进入 WAIT_DESIGN_SYNC。因此 sections 宜用标题，正文分散在表格行和段落时引用其所在标题。旧台账未存章节哈希时仍按整文件判断。

以下字段描述init/plan_update的plan参数，不是正式台账文件内容：

| 字段 | 参数要求 |
|---|---|
| name | 任务名称 |
| roles | 支持五类程序职责标签；必填技术负责人的实际tool/model/effort；其他角色及handoff.backup可在用户授权下暂缺，之后经set_models首次登记；兼任用相同principal |
| batches | 批次ID对应name、completion_task，可选retired |
| subjects | 语义对象ID对应当前版本 |
| budgets | 预算ID对应正整数limit；init/plan_update 强制检查完整执行接管路径与独立审阅额度配套，规则见 protocol-budget.md |
| tasks | 任务ID对应下文任务字段和依赖 |
| decision_points | 裁决点ID对应signal、owner、blocks、instruction |
| feedback_minutes | 可选；阶段kind对应反馈分钟阈值 |
| context_groups | 可选；技术链路组ID对应不重叠批次ID数组，旧plan缺省保持兼容 |

默认常规通道示意（scope均为B01，版本和预算按本次计划填写）：

| task | kind / role | subjects | depends_on | 其他输入 |
|---|---|---|---|---|
| B01.E | execute / executor | B01.code | 前置依赖 | instruction给目标/范围/AC/验证入口 |
| B01.R | review / reviewer | B01.code | B01.E | repair_tasks指向B01.E |
| B01.release | acceptance / orchestrator | B01.code | B01.R及其他必需门禁 | 不自动增加第三方 |

仅需前置决定的设计通道增加如下节点：

| task | kind / role | subjects | depends_on | consumes | 其他输入 |
|---|---|---|---|---|---|
| B01.D | design / executor | B01.design | 无 | B01.D | instruction指定卡片、约束与验证计划；session_policy为resume |
| B01.DR | review / reviewer | B01.design | B01.D | B01.DR | repair_tasks指向B01.D；session_policy为resume |
| B01.E | execute / executor | B01.design、B01.code | B01.DR | B01.E | instruction要求按已审卡片实现验证；session_policy为resume |
| B01.release | acceptance / orchestrator | B01.code | B01.E及其他必需门禁 | 无 | instruction要求核对全部必需证据 |

提交时必须提供完整、相互可解析的plan参数；本表不能作为文件原样传给程序。已跑通的分批init参数见[初始化示例](init-example.md)。

以上为设计门禁参数示意；实际任务还须按计划补齐实现审阅、第三方、验证或提交等节点，再让release依赖全部必需节点。批次卡约定与修订路径见 [batch-cards.md](batch-cards.md)。通用引擎只验证已配置的依赖，当前schema不要求专用design_task字段，也不自动识别缺失设计节点；技术负责人按本批通道配置，编排者开工前检查实际门禁，不把validate成功当作门禁配置完整的证明。

## 节点目标的结构化传递

plan 和各 task 可配置 `goal_contract`，固定形状如下；内容由技术负责人按本任务实际目标编制，不由派发者从旧报告、测试名称或历史阻断推断。

```json
{
  "goal": "本项目或本节点要得到的结果",
  "acceptance": ["可核验的验收项与适用场景，引用必要的AC/契约"],
  "non_goals": ["本阶段不要求的能力、异常场景或留给其他节点的验证"]
}
```

新计划应配置项目与节点两层目标。项目层描述总目标与跨阶段边界；节点层描述该动作的目标，不能将“基本功能”硬编码为所有任务的标准。例如同一项目功能批次只验基本闭环，可靠性批次可以要求故障注入和耐久。`goal` 为非空字符串，`acceptance` 为非空字符串数组，`non_goals` 为字符串数组（允许明确空数组）。启用项目 `goal_contract` 后，所有未 retired 节点都必须有完整节点目标，包括设计、实现、常规/第三方审阅、确认、提交和验收；缺项使计划校验失败，后续 plan_update 不得删除项目目标退回非结构化模式。

`flow.py status` 的每个技术 `subagent_payload.goal_context` 携带当前计划版本、项目目标、当前节点目标。审阅/核验/确认/验收只携带直接覆盖的交付节点（design/execute/verify）的目标，不递归展开前置审阅或确认链；诊断与裁决携带决定 blocks 和 task 恢复条件的节点目标。只复制有效计划，不继承历史缺陷等级。无绑定节点的动作明确显示缺少节点依据，不自行推断。依赖节点的验收是覆盖背景，非要求当前动作提前完成所有下游验证。

`dispatch_card.py` 在工作指令之前优先呈现当前节点目标/验收/非目标，再附直接覆盖对象的验收边界和一次项目约束；相同条目仅出现一次；CLI、原生子代理、负责人移交和续接共用此入口。完整载荷保存为卡目录的 `payload.json`，不在提示词中重复输出JSON；固定指令只出现一次，不按长度截断命令、路径或验收项。派发卡记录目标上下文哈希；未开始卡在计划/目标变化后重建，已登记但未调用的旧卡停止发送并交责任人处理，不撤销在途工作或重置投入。调用原生工具时原样发送 `subagent_payload`，不能只摘 instruction；手工编写 CLI 提示不具备自动注入保证，应使用派发卡。

旧台账可继续读取和使用，提示词显式标注尚未结构化并附节点原始指令及设计定位；不会自动声称已解析出目标。技术负责人在下一次正式 plan_update 中逐节点迁移，不要求重建历史 attempts。校验能保证字段和传递完整，不能判断目标内容是否合理；阻断仍须映射当前AC、支持场景和实际影响。项目目标、节点目标、设计书、instruction发生实质冲突须先裁定并同步，不能用新字段暗中修改验收承诺。

## 批次指令与设计书对应

经技术负责人判定的小任务可省略完整设计书，但目标、方案要点、完成标志及验证入口仍须齐备。分批方案中，技术负责人交付init/plan_update时，每批实现任务的instruction必须直接包含“本批目标、方案要点、完成标志”，再给出设计书准确路径、阶段/批次章节和对应计划版本。仍保留必要的允许范围、前置条件、验证入口及已有约束。instruction是现有字符串字段，不新增goal/solution/acceptance等schema字段，不只填批次ID、文档路径或“按设计执行”。主执行者、备选及负责人兜底读取相同的当前批次指令。

推荐在instruction字符串中使用以下简短结构（内容由技术负责人填写，不是新增JSON键）：

```text
本批目标：要解决什么问题、得到什么结果。
本批方案：改哪一层、复用什么、如何衔接，以及必要的取舍/约束。
完成标志：哪些可核实结果代表本批完成；对应验收与验证入口。
设计依据：<设计书准确路径>，<阶段/批次章节>，<对应计划版本/已有固定版本引用>。
允许范围及前置条件：<已有约定>。
```

设计书保存面向用户的解释，instruction保存足以执行的要点；不复制整份设计书、重复建立批次报告。技术负责人从同一次规划生成两者并核对一致，方案实质改变时经正式plan_update更新指令，同时更新设计书相关部分；只改Markdown不会自动同步JSON。需要用户重新确认的变化仍按既有授权/裁决规则。

编排者首次派发执行任务时原样包含该task.instruction及设计书定位信息，不能自行摘要掉方案/完成标志或只给任务ID。续接只补已生效变更和本轮必要输入；换备选/兜底时重新给当前完整指令与既有问题证据。编排者仅机械核对三项和引用是否提供，不代写技术内容，缺项退技术负责人补齐。执行者先核对当前instruction并按需读所引用的本批章节，无需通读全份设计书；发现实质矛盾或必要依据不可用时上升技术负责人，只暂停受影响路径，不自行择一执行或同步文档。

脚本当前仅校验instruction为非空字符串，不解析这三项语义或自动同步文档；validate通过不代表已完成设计/执行指令的一致性检查。此项由技术负责人交付自查、编排者派发核对及执行者实际读取落实，不为格式检查追加模型审阅。

## 合并审阅与阶段触发

审阅节点是kind=review或third_party的普通task，不需要另建评审组实体或文档。scope列覆盖批次，subjects绑定全部被审对象版本，depends_on列触发所需的实现/验证任务，repair_tasks明确可返修任务。技术负责人在instruction写检查目标与放行范围，并将对应后续工作或completion_task依赖该审阅。

在能守住AC、关键契约及返工成本的前提下优先合并，尽量减少独立审阅节点。下面为依赖示意，非完整init参数；单批E→R示例不表示每批必须有专属R：

| task | scope / subjects | depends_on | 作用 |
|---|---|---|---|
| B1.E | B1 / B1.code | 既定前置任务 | 实现与验证 |
| B2.E | B2 / B2.code | B1.E（若有实现依赖） | 无需等待尚未到达的组审阅 |
| R12 | B1、B2 / B1.code、B2.code | B1.E、B2.E | 一次常规审阅覆盖两批；repair_tasks指向B1.E、B2.E |
| B3.E | B3 / B3.code | R12 | 需已确认契约后实施的关键集成与验证 |
| T123 | B1、B2、B3 / 三批被审对象 | B3.E及其他必要验证 | 链路形成后一次独立第三方审阅 |
| release | 受本节点放行的批次 / 相应对象 | R12、B3.E、T123及其他必需门禁 | 核对全部目标与证据后放行 |

此例不强制另加B3常规审阅：阶段第三方可承担约定的独立实现审阅覆盖。若常规和第三方都必要，则分别配置节点与依赖，各自独立检查，不互相替代；不要仅为流程形式双审。第三方必须全新上下文，不输入常规审阅结论；可依赖常规已通过以控制节奏，但独立性来自输入和检查方式。

批次实现完成不等于批次DONE。共享放行任务或各批独立放行任务均可，关键是所有必需审阅进入对应依赖闭包。审阅依赖成员实现/验证，不依赖把它作为放行条件的completion_task，避免循环。对象版本变化使覆盖该版本的组审阅失效，不得沿用旧结论放行。已有计划调整仍经plan_update和预算协议，不能借合并删除既有阻断或清零预算。

roles中的模型来自本任务用户指定或明确授权选型；入口提醒和来源核对由编排者按 [prompts.md](prompts.md#0-编排者入口模型提醒) 执行。当前脚本校验模型配置/实际调用一致，不验证用户消息授权，也不检测新编排会话；不能宣称已有roles数据就完成了用户指定检查。来源引用放在已有提示/规划证据中，不新增schema字段。

Linux CLI 配置中 effort=default 表示继承 CLI 自身配置/模型默认值，不是传给远端的 effort 字面值，也不等于固定 medium；适配器省略覆盖参数并保留请求值。明确 effort 值按已批准配置传递；实际支持及派发前检查见[固定入口](linux-cli.md)。

## 用户指定模型的直接登记

计划可以先于角色模型登记：用户授权落盘计划后，技术负责人只填自己的配置即可init。任务所需职责（含备选阶段）缺配置时，status显示WAIT_MODELS并给编排者ASSIGN_MODELS动作（missing标出缺的职责或备选任务；status.unassigned_models一次列出全部缺项，供技术负责人核对），start拒绝派发，pre_yield视为CONTINUE；next先派负责人登记明确的用户条件，只有正式owner=user待办才询问用户；已有用户指定原话仍可直接经set_models登记，相应任务转READY。model 字段原样传给 CLI（脚本不改写、不替换），登记时须写该后端实际接受的完整模型ID或官方别名：用户口语说法按其本意规范化（如“opus5.5”登记为 `claude-opus-5-5` 或别名 `opus`），并向用户复述映射；无法确定对应哪个模型时询问，不自选替代、不按旧模型列表猜。`agent_cli.py check` 不调用模型，模型名错误要到实际启动才暴露：启动即报模型不存在/无权限属环境失败（INVALID，未开展实质工作），修正登记后重派，不计技术轮次。

用户明确为已有任务指定模型时，编排者使用`set_models`直接登记，成功后告知变更及生效范围即完成这次模型调整，不额外创建signal、架构调用、resolve/close或计划评审。该入口只处理调用配置；涉及职责重分配、技术方案、预算、依赖、验收或授权的变更仍走原协议，不能把混合变更包装成用户选模型。

输入必有`roles`、`backups`、非空`reason`和非空`user_evidence`路径数组。roles以角色名为键，backups以已有handoff的任务ID为键；各值为非空局部配置，仅支持`model`、`tool`、`effort`非空字符串，只传用户明确指定的项。至少一项真实变化。计划已用到但尚未配置的角色、或尚无backup的handoff，可在此首次登记，此时必须同时给出tool、model、effort；计划未用到的角色仍须经plan_update加入。principal、主/备/兜底预算ID、限额及任务内容均不可修改；已激活的handoff可以更新backup调用配置，但不会恢复备选机会或回退接管阶段。user_evidence存用户原始答复或既有原生记录，由编排者核实用户确实指定了这些项；脚本只校验文件存在并固定哈希，不认证来源或推测语义。用户只授权选型而未明确具体配置时，不以本入口自行冒充用户指定，仍按原有授权/规划方式安排。

```text
flow.py set_models --actor orchestrator --revision <REV> --data '{"roles":{"executor":{"model":"<用户指定模型>"}},"backups":{},"reason":"按用户本次明确指定调整主执行者模型","user_evidence":["<用户原始答复路径>"]}'
```

`backups` 以已有 handoff 的任务为键（备选挂在该任务的接管预算上，不是全局角色）。用户说“全部未完成任务”时可写 `"backups":{"*":{...}}`：脚本展开为所有有 handoff 且未 SATISFIED/RETIRED 的任务，显式任务键覆盖通配。向用户确认时问“挂在哪些任务/是否全部未完成任务”，不要求用户给任务 ID。

操作复用CAS、幂等、原子事务及history；plan_version递增，state.plan_changes追加kind=user_model_update、用户依据哈希、原因及原/新计划哈希，不需要decision_id或架构作者context。此次编排登记不把当前会话标为技术计划作者，不使既有审阅仅因选模而失效；已存在的其他裁决与门禁不自动关闭。

所有已开始/结束attempt的实际模型、performer_config、版本、证据、计时和预算保持原样。变更影响后续调用，正在运行的任务按原配置交付；不在台账冒改为新模型，不因改模型自动打断/重派或清零技术预算。明确需要中断换模时按用户意图与当前写入状态处理，并保留原调用结果。后续start按新的status.call_config核对宿主实际可用性；如工具/模型改变导致原会话不支持合规续接，以replacement_reason记录新上下文，不能借此自审、重领轮次或静默回退模型。主会话实际模型由宿主控制，set_models不能替用户切换宿主配置。

旧task.instruction或设计书若夹带已被用户取代的模型文字，派发头明确引用本次用户指定及当前call_config作为模型依据，其余技术指令原样传递；不要求为更新这些文字先让技术负责人重写方案。无需额外模型配置台账。

roles 的各配置可选 principal。architect 与 reviewer 配置相同 principal，表示同一技术负责人兼任两种职责，actor仍分别登记，合并不删除历史角色标签，也不合并审阅结果与裁决记录。程序不内置启动命令。

任务 kind：design/execute/verify/review/third_party/commit/acceptance/confirmation。任务可选phase（非空字符串）仅作统计分类；默认及追踪规则见 [tracking.md](tracking.md)。review 和 third_party 的成功结论为 RELEASE，其他为 VERIFIED。依赖要求目标的有效成功结果，不支持将自然语言或任意表达式当作可执行条件。

subjects 绑定语义对象版本；重规划不修改已存尝试的版本。change_subject 更新当前版本，旧版本结果自动失效。若版本变化涉及多层，技术负责人明确列出全部受影响 subject，不由程序猜测。

每个任务 scope 是批次ID，subjects 是对象ID，consumes 是预算ID（可多个，共享记账），depends_on 是任务ID。reviewer R2 默认配置 resume；第三方配置 independent。可选 repair_tasks 指定审阅/机械核验 FIX_REQUIRED 后的修复任务；程序要求修复交付发生在该审阅尝试之后，先提示 REPAIR 再允许复核。未配置归属时提示 PLAN_REPAIR，不直接重复审阅。修复任务仍使用原预算ID。可选 can_close 指定本任务能明确复核关闭/重新确认的既有任务，finish.closes 写对应原始 attempt ID。可选 decision_only=true 表示仅在裁决恢复条件引用时激活，不干扰正常主流程。

可选 retry_limit 默认1；无效调用用 retry_of 原始尝试ID重试，不重置技术预算。requires_authorization 默认true；retired 默认false。不要将预算或所有者从任务中删掉来规避既有消耗。

decision_points 对象按 ID 存储，每项 signal/owner/blocks/instruction；owner 为 architect 或 user。blocks 是精确受阻任务，不要阻塞其自身必须执行的恢复任务。未预设 signal 可以直接登记。budget_exhausted、scope_blocked、severe_block信号及SEVERE_BLOCK裁决点的实际处理人固定为architect，即使旧配置或历史pending记录owner=user也先交技术负责人诊断；新signal登记architect，旧记录只在查询/resolve校验时按有效职责处理，不改写历史owner。是否请求用户决定，由技术负责人通过resolve的用户确认恢复条件明确提出。技术阻塞或预算耗尽不自动生成等待用户动作；纯authorization_required等用户授权节点仍按原配置处理。
