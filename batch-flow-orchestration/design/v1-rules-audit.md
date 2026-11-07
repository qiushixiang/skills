# v1 角色规则审计：哪些带入 v2

本文逐条核对 v1（标签 `v1-final`）中的角色专业规范，判断它们在 v2（工单驱动）中的去向，供用户决定恢复哪些。纯编排机制（台账操作、派发、编排者红线、宿主）只在含有角色规范时列出。分类含义：
- **保留**：原意仍成立，可基本照写进 v2 文档。**改写**：仍成立，但要换成 v2 的术语或机制重新表述。
- **脚本承担**：v2 已经用脚本或设计结构保证，写明具体机制。**删除**：v2 中已不适用，写明理由。
- **缺口**：仍成立且重要，但 v2 文档里没有，脚本也不保证。

来源缩写（v1）：SK=SKILL.md，TL/EX/TP/OR=references/roles/ 下的 technical-lead/executor/third-party/orchestrator，EN=engineering.md，AR=artifacts.md，CX=contexts.md，PR=prompts.md，PP=protocol-plan.md，PD=protocol-decisions.md，TD/PS/RV/RC=workflows/ 下的 triage-direct/plan-sync/review/recovery。v2 的 design §n 指 `design/workorder-redesign.md`。

## 1. 任务开始与分流

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| A1 任务目录由用户指定，没指定先问；不按源码目录、修改时间推定，不自动搜索或切换任务（SK；OR 开工） | 改写 | 所有命令都要显式 `--base`，前端在 launch 时绑定；但技术负责人编计划时“先问任务目录”这一条没写 | technical-lead.md「开工」一句 |
| A2 任务目录与源码工作目录分开写明（SK；AR） | 脚本承担 | `plan.workspace`；适配器 `--cwd` 取它，缺失就报错（design §19.3） | — |
| A3 新任务先由技术负责人核查目标、范围、验收、依赖（SK；TD） | 保留 | plan.md 开头有一句 | technical-lead.md「开工与分流」 |
| A4 局部直派还是分批：能局部验收、不需要跨组协调、也没有前置契约要定，就直派（TD） | 改写 | v2 没有 local 模式，所有任务都走 plan.json | technical-lead.md：小任务 = 单批计划（1 个交付目标 + 1 个审阅组），可不写设计书 |
| A5 小任务用最小任务说明：目标、范围、验收、前提、证据入口（TD；PP 批次指令） | 改写 | 对应交付目标的 instruction/acceptance 字段 | plan.md「交付目标写法」 |
| A6 局部任务也不能省略独立审阅（TD） | 缺口 | plan.md 写了“每个交付目标至少属于一个组”，但 model.py 不检查；没有组的交付目标根本不会开 review | 脚本检查：计划校验（见末节第 1 条） |
| A7 局部任务由技术负责人当唯一 dispatch_owner，自己 start、回收、pre_yield（TD） | 删除 | 派发由驱动器承担（design §6） | — |
| A8 转为分批时保留已有代码、证据、预算和历史（TD；RC） | 脚本承担 | `revise` 保留已有工单和 run，over_budget 不让已用额度被收回（register.revise） | — |
| A9 已批准计划里的正常节点继续推进，不重新分流（SK；TD） | 脚本承担 | 由路由表开立工单（design §5.2） | — |
| A10 用户没指定主会话角色时，主会话担任编排者；已派发的子会话保持派发身份（SK） | 删除 | 没有模型编排者；角色身份由工单卡决定 | — |
| A11 维护本技能时不递归启动项目编排（SK） | 保留 | v2 SKILL.md「维护本技能」一行 | 维持现状 |
| A12 授权、模型来源、预算有缺项时不推定（TD；SK） | 改写 | init/revise/set-role 都要用户原话文件；没写“编计划时不推定模型/额度” | technical-lead.md「开工」 |
| A13 模型来自本任务用户指定或明确授权的选型；不能从旧任务、示例、计划字段推定；只补即将用到的缺项，不让用户重报全部（SK；PR §0；PP） | 改写 | 运行中换模型用 `set-role`（需原话）；init 时 roles 由负责人手填，没有来源规则 | plan.md「roles」一节 |
| A14 用户口语说模型名时先查目录再解析，唯一结果才登记，多个候选再确认；只授权“选型”不等于指定了具体配置（PP 用户指定模型） | 改写 | cli-adapter.md 和 `set-role` 覆盖运行中；init 前没写要先 resolve | plan.md「roles」：写 roles 前用 `models`/`resolve-model` |
| A15 模型不存在或无权限属环境失败，登记 INVALID 免扣，修正后重派（PP） | 脚本承担 | 适配器 invalid → `abandon` 免扣；连续失败就转运维（design §19.3、§22） | — |
| A16 已授权的闭环不用等用户口头提醒才继续（TD） | 脚本承担 | 驱动器事件驱动推进（design §5.4） | — |

## 2. 批次设计与计划

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| B1 用户审阅通过后才 init，原话保存成文件（SK；PS） | 脚本承担 | `init/revise --approval` 必须是非空文件，记录哈希（model.approval_ref） | — |
| B2 用户审阅通过就是授权负责人登记计划，不用再单独请示（TL；PS） | 保留 | plan.md 隐含 | technical-lead.md「计划提交」一句 |
| B3 JSON 初始化、文件存在、validate 成功，都不等于用户已评审，也不证明门禁配置完整（AR；PP） | 保留 | 无 | technical-lead.md「计划提交」 |
| B4 实现批次和审阅节点分开规划；相关的低风险批次优先合并审阅；只有关键契约要在扩散前确认、返工代价明显上升或形成独立验收边界时才拆节点；不固定逐批双审，也不全部拖到最后（EN 审阅边界；PP 合并审阅） | 缺口 | plan.md 只写了审阅组的字段 | technical-lead.md「批次与审阅节奏」 |
| B5 每项实现都要被约定的独立审阅覆盖（EN） | 缺口 | 同 A6，model.py 不检查 | 脚本检查：计划校验 |
| B6 第三方只在计划的必要节点安排；常规和第三方都需要时分别配置，不互相替代，也不为形式双审（EN；PP） | 改写 | review_groups 的 `reviewer: third_party` | technical-lead.md「批次与审阅节奏」 |
| B7 第三方可以排在常规审阅通过之后，以控制节奏（PP 合并审阅） | 缺口 | 审阅组不能依赖另一个审阅组，两个组都只等成员 implement；常规审阅返修后，第三方可能已经审过旧版本 | 与 G19 一起解决；或给 review_groups 加 `depends_on` |
| B8 审阅组在 instruction 里写检查目标和放行范围（例如集成批次只按集成目标放行）（PP 合并审阅） | 缺口 | review_groups 没有说明字段，审阅卡只列成员的验收 | plan.md：给 review_groups 加 `focus` 字段，cards.py 渲染进“本单” |
| B9 批次实现完成不等于批次 DONE；所有必需审阅都要进入放行的依赖闭包（PP） | 脚本承担 | release 等待批次内全部 implement/review 和 triage（routing.sync_plan、link） | — |
| B10 审阅只依赖成员的实现，不依赖放行任务，避免循环（PP） | 脚本承担 | release 自动生成；有循环检查（model.normalize） | — |
| B11 项目和节点两层目标（goal/acceptance/non_goals）；节点目标不能一律硬编码成“基本功能”；可靠性批次可以要求故障注入；各阶段单独确定验证强度，不从旧反例推出新门槛（PP 节点目标；PS） | 改写 | v2 项目层 `goal{objective,constraints,non_goals}`，交付目标层只有 acceptance，没有 non_goals | plan.md：交付目标可选 `non_goals`；technical-lead.md 写“分阶段定验证强度” |
| B12 有项目目标时，每个节点必须有完整目标，缺了计划校验就失败（PP） | 缺口 | model.py 把 instruction 默认成 `''`，acceptance 默认成 `[]`，卡片显示“见指令” | 脚本检查：计划校验 instruction 和 acceptance 非空 |
| B13 每批 instruction 必须写本批目标、方案要点、完成标志、设计依据（路径/章节/计划版本）、允许范围和前提；不能只写 ID、路径或“按设计执行”（PP 批次指令；AR） | 改写 | instruction 是自由文本，另有可选 `design_ref` | plan.md「交付目标写法」：给五行模板 |
| B14 主执行、备选、兜底读同一份当前指令（PP） | 脚本承担 | 同一 implement 工单渲染同一张卡（cards.render） | — |
| B15 目标、节点目标、设计书、instruction 有实质冲突时，先裁定再同步，不能借新字段暗改验收（PP） | 保留 | 无 | technical-lead.md「计划修订」 |
| B16 实质变更先改设计书和 instruction，再一次 revise；只改 Markdown 不会同步到 JSON（PS；PP） | 改写 | `revise --approval`；没写顺序 | technical-lead.md「计划修订」 |
| B17 审阅额度要覆盖主执行、备选、兜底可能产生的交付轮次；不能先返修，回头再向用户要复核额度（SK；PS） | 改写 | v2 额度用尽 → decide → grant 转用户（design §8）；不会卡死，但可能频繁打扰用户 | technical-lead.md 一句；可选做成计划校验的警告（末节第 10 条） |
| B18 不能删掉预算或所有者来规避已有消耗；重排计划不能清除既有阻断（PP） | 脚本承担 | revise 时 over_budget 拒绝收回已用额度；修订本身要用户原话 | — |
| B19 关联技术链路配成同一 `context_groups`，无关领域分开（CX；PS） | 删除 | v2 按交付目标链续接 decide 会话（context.session_for） | — |
| B20 前置设计通道 D→DR→E→R：只在有具体前置问题时使用（PP；PR §6；RV） | 改写 | 没有通道概念 | plan.md：写成“设计”交付目标 + 审阅组，实现目标 `depends_on` 该组 |
| B21 计划变更时向用户报告改动的章节、影响和待决事项（PS） | 保留 | 无 | technical-lead.md「计划修订」 |
| B22 本地提交节点依赖它所提交内容的审阅（PP 示例） | 脚本承担 | depends_on 可以引用审阅组（design §19.1），plan.md 有写 | — |
| B23 CLI 执行环境（require_host、add_dirs、受控入口、preflight、blocked_output）按后端配置，不按批次（PP） | 脚本承担 | `plan.env`；卡片“环境”节；gate.environment_blocked | — |
| B24 编排者首次派发时原样带上 instruction 和设计定位，只机械核对三项是否提供（PP） | 删除 | 卡片由工单渲染；编排者不存在（核对改由末节第 2 条计划校验承担） | — |

## 3. 设计书

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| C1 分批方案必须交付一份人类可读的 Markdown 设计书，不能只交 JSON 或一句“规划完成”（AR） | 缺口 | v2 只有 `plan.design{path,sha256}` 字段，没有任何设计书规范 | design-book.md |
| C2 位置：优先写进已有总体设计文档的固定章节，没有再用 `<任务根>/design.md`；一份总表，不逐批增加报告或单独的 review-plan（AR） | 缺口 | 无 | design-book.md「位置」 |
| C3 用户评审设计书不等于模型技术审阅；总体设计默认不加技术审阅节点，有具体风险才说明理由、范围和放行条件（AR；CX） | 保留 | 无（v2 本来也没有计划审阅节点） | design-book.md 一句 |
| C4 写给要判断方案的用户：先用自然语言说明做什么、为什么、做到什么程度，再分阶段总览和逐批计划两层；每个批次单独一行（AR） | 缺口 | 无 | design-book.md「结构」 |
| C5 用名称和动作说明，不拿代码定义代替解释；内部 ID、字段、函数、缩写首次出现时说明用途；正文不用 D→DR→E、depends_on 这类流程语法（AR） | 缺口 | 无 | design-book.md「写法」 |
| C6 模板：总体方案 / 阶段总览 / 逐阶段批次表（目标、方案、完成标志、常规审阅安排、第三方审阅安排）/ 需要用户决定的事项 / 必要技术索引（AR） | 缺口 | 无 | design-book.md「模板」（照搬 v1 模板） |
| C7 模板只示范写法，不照搬阶段数和审阅节奏；没有第三方就写“不安排”；合并审阅要写明和哪些批次合并、在哪批之后、覆盖谁（AR） | 保留 | 无 | design-book.md「模板」 |
| C8 每批方案要有实际内容；“适配某模块”“完成接入”“按契约落地”不算方案；还没定的写当前建议、为什么暂定、缺什么事实、在哪批决定（AR） | 缺口 | 无 | design-book.md「写法」 |
| C9 验收用用户能理解的结果表达，再附验证入口；表格要便于浏览，长说明放到批次下面的短段落（AR） | 保留 | 无 | design-book.md「写法」 |
| C10 交付前自查：不打开源码和 JSON，用户能否说出有哪些阶段和批次、各自的目标和方案、每次审阅覆盖什么（AR） | 保留 | 无 | design-book.md「自查」 |
| C11 设计书里的阶段/批次与计划 ID 要对应，但不能把 JSON 原样翻成 Markdown（AR） | 改写 | 交付目标有 `design_ref` | design-book.md：批次名后括注交付目标 ID |
| C12 设计书写解释，instruction 写执行要点，两者在同一次规划里生成并核对一致，不复制整表（AR；PP） | 改写 | 同 B13 | design-book.md + plan.md 互相引用 |
| C13 更新约束：唯一入口、锚点和批次编号稳定；只改受影响部分；已完成批次保留当时方案；待定建议放在固定待决区；只简记实质变化的原因和影响；不写运行进度，不另建修订台账（AR；PS） | 缺口 | 无 | design-book.md「更新」 |
| C14 每次交付给用户：章节链接、改动和影响、需要决定的事项；实施前取得用户对方案和推进范围的确认（AR） | 改写 | approval 文件机制已有 | design-book.md「交付」+ technical-lead.md |
| C15 被引用章节的哈希一旦变化，相关任务进入 SYNC_DESIGN，暂停新派发；其他章节可以自由修改（AR；PP 计划结构；PS） | 缺口 | `design.sha256` 写入了计划，但没有任何代码检查；design_ref 只渲染进卡片 | 脚本检查：设计漂移（末节第 3 条），或干脆删掉 sha256 字段，避免让人误以为有保护 |
| C16 纯文字纠错用 design_refresh，不增加计划版本（AR；PS） | 删除 | 要看 C15 怎么定：如果做章节哈希检查，就需要一个“刷新哈希”命令（要原话） | 随 C15 |
| C17 执行者不负责编写和同步设计书（AR） | 保留 | 无 | roles.md 执行者一句 |
| C18 脚本不能判断自然语言是否一致，也不能判断用户是否确认，这由技术负责人负责（AR；PS） | 保留 | 无 | design-book.md 末句 |

## 4. 技术负责人（裁决、恢复、兜底）

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| D1 身份：规划、技术决定、独立常规审阅、必要时兜底；architect 和 reviewer 两种职责可以由同一人兼任，但上下文要隔离（TL；SK） | 改写 | v2 把审阅拆成独立的 reviewer 角色；负责人只做 decide、triage 和兜底（roles.md） | technical-lead.md「身份」 |
| D2 按派发的工作模式办，收到审阅任务不能转去做规划（TL） | 脚本承担 | 卡片类型决定要做什么（cards._body） | — |
| D3 普通缺陷直接按归属返修；只有技术方向、预算、范围、重要契约或严重阻断才升级到负责人（RC） | 脚本承担 | 路由表：FIX_REQUIRED → repair；待裁决/外部阻塞/用尽 → decide（design §5.2） | — |
| D4 先诊断再处置：分清是实现/环境问题、方案缺陷，还是授权/范围确实要变；不能没诊断就加预算，也不能把技术问题推给用户（PD 严重阻塞） | 缺口 | roles.md 只写“写出诊断” | technical-lead.md「裁决」第一条 |
| D5 别人列出的“待关闭问题”不能直接当成阻断；先核实它是否真的违反当前目标（PR §1） | 缺口 | 无 | technical-lead.md「裁决」 |
| D6 给出原因、影响和最小恢复条件；恢复条件必须能检查（RC；PD 裁决恢复条件） | 改写 | decision.json 的 diagnosis + actions，actions 本身就是可执行的恢复条件（design §7） | technical-lead.md「裁决」：diagnosis 写原因、影响和为何选这些动作 |
| D7 同一技术链路续接决策上下文；跨组契约交总体协调（RC；PR §3） | 脚本承担 | 同一交付目标上的 decide 续接上一个决策会话（context.session_for） | — |
| D8 实质变更走计划同步；新增授权或预算按用户原始来源办（RC） | 脚本承担 | plan_revision、grant 和超出包络的 open 一律转 ask_user（register._needs_user） | — |
| D9 workaround 要局部、可撤销、可验证；写清为什么能解决、涉及的对象和路径、预期投入和停止条件、兼容或临时影响、撤销办法、必要验证（PD；RC） | 缺口 | 无 | technical-lead.md「workaround」 |
| D10 “小代价”不是无限修复许可：不擅自追加额度、放宽验收、改变公共契约或越过修改边界（PD） | 改写 | 加额度和改计划由脚本拦下；但包络内 `open` 的 input.instruction 是自由文本，可能悄悄扩大范围或改契约 | technical-lead.md「workaround」 |
| D11 有直接证据证明方案不成立时可以直接否决，不为走流程硬做一次（PD） | 保留 | 无 | technical-lead.md「裁决」 |
| D12 先尝试恢复被阻塞的批次，确实不行才转去做其他批次（PD；RC） | 删除 | 驱动器只让依赖它的工单等，其余照常推进；decide 优先级最高（design §5.4） | — |
| D13 不能只批准 workaround 的文字就放行，必须真实实现、验证、审阅（PD） | 改写 | release 要等 implement 和 review 完成；但 decide 可以 supersede 一张 review，结果被当成已满足（见 D17） | technical-lead.md 一句 + 末节第 5 条 |
| D14 不能改脚本校验或直接编辑台账来绕过门禁（RC） | 保留 | v2 SKILL.md 硬规则“状态文件只经 bf 写入” | 维持现状 |
| D15 需要用户决定时，给出已诊断的问题、推荐方案、代价和影响，只保留真正待决的项（PD） | 改写 | ask_user 动作有 `options` 字段 | technical-lead.md「向用户提问」 |
| D16 确实无法恢复时，记下依据和剩余最小缺项，本批保持未通过，下游保持受阻（PD） | 改写 | 可以用 ask_user 或保持工单开立；没写要求 | technical-lead.md「裁决」 |
| D17 不把无关任务列入 blocks，也不跳过依赖让下游虚假 READY（PD） | 缺口 | decide 只挡自己的目标（routing.runnable）；但 `supersede` 会让所有依赖方视为已满足，包络内无需用户就能执行，相当于跳过依赖或审阅 | 脚本检查：末节第 5 条 |
| D18 实施、返修、验证照常扣预算，不能借临时方案免扣或重置（PD） | 脚本承担 | 免扣只适用于可机械核验的环境失败（api.deliver 的 exempt 条件，design §7） | — |
| D19 兜底另开作者会话，保留预算，不得自审（TL；TD；RC） | 脚本承担 | 执行阶段变化就开新会话；审阅排除作者会话（context.py I4） | — |
| D20 兜底实施按执行者规范做（TL） | 保留 | roles.md 有一句“作为执行者按 implement 卡工作” | 维持现状 |
| D21 非阻断建议在交接边界统一处置：本轮修、纳入后续批次（必要时标出最迟节点）、或不采纳；收口前确认去向，不要求全部修（EN；PR §3；RC） | 改写 | triage 只处理“候选新项”（merge/defer/drop）；审阅者写的“非阻断建议”不进入任何路由，会丢失 | technical-lead.md「分诊」+ 末节第 9 条 |
| D22 只有改变计划或承诺时才正式登记决定（EN） | 脚本承担 | 只有 decide 工单产出 decision.json | — |
| D23 交付：方案或结论、待定事项、改动章节和影响、原始证据、下一责任人（TL） | 改写 | decision.json 的 diagnosis | technical-lead.md「裁决」：diagnosis 的要素 |
| D24 WAIT_EVIDENCE 机械登记后派负责人诊断，不凭等待报告去请求用户，也不重复原任务（PD；RC） | 脚本承担 | WAIT_EVIDENCE → decide（register.on_registered） | — |
| D25 等待证据的诊断要区分“测试真的没运行”和“只是 record 没落盘”；恢复条件只能选只读环境能跑的验证（PD） | 改写 | 外部阻塞只认受控入口的环境输出（gate.environment_blocked） | technical-lead.md「裁决」：wait_evidence 的判断要点 |
| D26 裁决对下一轮的一次性要求（换新会话、附一句提示）写进结构化字段，不写在正文里；持续有效的要求写进指令（RC；PD resolve.dispatch） | 脚本承担 | actions 的 `fresh_session`、input.constraints 渲染进卡片“约束”节 | technical-lead.md 一句：长期要求走计划修订 |
| D27 纯澄清（clarification）、repin、supersede_decision、confirmation_waits（PD） | 删除 | design §7 明确删除 | — |
| D28 严重阻塞和预算耗尽固定交负责人，不自动生成用户等待（PP decision_points） | 脚本承担 | 用尽的唯一出口是 decide（design §8） | — |

## 5. 执行者

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| E1 按当前指令实现、验证、定向返修（EX） | 保留 | roles.md | 维持现状 |
| E2 第一个动作核对目标、范围、前提、验收、版本和预算（EX） | 改写 | 卡片有目标、验收、上一轮；没有额度余量 | roles.md 执行者第一条；可选：卡片加一行“本单额度 x/y” |
| E3 输入只有卡片和指令引用的设计章节，不加载技能参考文档（EX） | 脚本承担 | roles.md 写明“工单卡是唯一输入” | — |
| E4 按需读所引用的本批章节，不通读整份设计书（PP） | 保留 | 无 | roles.md 执行者 |
| E5 常规直接实现；只有确实存在未决的前置契约时才做最小设计并等审阅（EX；PR §6） | 改写 | 见 B20 | roles.md 一句：设计类交付目标按卡片要求交设计 |
| E6 交付实际差异和真实可复跑的验证；已有原始结果直接引用（EX） | 保留 | 卡片“改动与版本”“验证证据”行 | roles.md / engineering.md |
| E7 遇到方案矛盾就上升，不自行改门禁、预算、模型或台账（EX；PR §6） | 脚本承担 | 卡片里的待裁决边界（cards.BOUNDARY）；执行者没有写状态文件的入口 | — |
| E8 发现实质矛盾或必要依据不可用：上升，只暂停受影响路径，不自己挑一个执行，也不去同步文档（PP） | 改写 | 待裁决边界只列了“无权改动的内容” | roles.md 执行者「待裁决」加“依据矛盾或缺失” |
| E9 不读写正式台账，不调管理命令（EX） | 保留 | roles.md 首段 | 维持现状 |
| E10 必需验证用 record 运行；自述、手抄、摘录的输出不算证据（EX；EN） | 脚本承担 | gate.cited_evidence 只接受 records/ 下符合结构的文件（evidence.is_evidence） | — |
| E11 按固定交付形态回复，停止原因如实填，没做完写“未完成”（EX；PR §8） | 脚本承担 | gate.judge 按停止原因和结果校验，不合法就续跑（design §10.3） | — |
| E12 “失败/未运行”字段不等于允许提前结束（PR §6） | 脚本承担 | “未完成”会续跑（api.deliver 的 continue_limit） | — |
| E13 有未决事项或用户叫停时保留现场（EX） | 保留 | 无 | roles.md 执行者 |
| E14 同批内可以续接，跨批默认新会话（EX；CX） | 脚本承担 | context.session_for | — |
| E15 返修只处理列出的阻断项，逐项关闭；已审过的部分不动（RV；v2 新增） | 脚本承担 | 返修卡；gate 检查每项都有“阻断关闭”行；“已审部分不动”只靠自觉 | roles.md 已有 |
| E16 范围外的问题写进候选新项，不顺手修（v2；对应 v1 EN 的“非阻断另列”） | 脚本承担 | gate.candidates → triage | — |
| E17 不同步卡片、交付报告、证据索引、进度表；不建 prompt/session/summary 文件（AR；PR §8） | 保留 | 无 | roles.md 执行者一句 |
| E18 外部阻塞要有受控入口的原始环境失败（v1 交付门禁；PR §6） | 脚本承担 | gate.environment_blocked | — |

## 6. 工程与证据

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| F1 第一证据是实际实现代码，以及针对验收目标、可重复运行且实际通过的构建/测试（SK） | 缺口 | v2 SKILL.md 只定义了证据的文件格式，没写这条原则 | engineering.md 第一句；SKILL.md 硬规则引用 |
| F2 方案要写明目标、非目标、已有契约和验收边界（EN） | 保留 | 无 | engineering.md「复杂度」 |
| F3 优先复用已有能力；不为未来扩展预建框架、平台或抽象（EN） | 保留 | 无 | engineering.md |
| F4 抽取帮助函数要有实际复用、独立概念或隐藏复杂边界的价值；一次性的简单逻辑不拆散（EN） | 保留 | 无 | engineering.md |
| F5 未知输入在负责它的边界验证一次；契约已保证的值不层层防御；异常只处理可恢复的情形，不吞错制造成功（EN） | 保留 | 无 | engineering.md |
| F6 假想的极端并发、安全或资源问题不能自动扩大范围；新增复杂度前说明可达的失败、发生条件和代价；超出授权要用户决定；已有安全契约仍须满足（EN） | 保留 | 无 | engineering.md |
| F7 遵循目标仓库的语言和工程规范，不把某个项目的风格推广为通用门禁（EN） | 保留 | 无 | engineering.md |
| F8 复用已有验证，只为验收、原缺陷反例和直接影响补测试；不凑类别、数量、覆盖率；不镜像实现；不建通用测试平台（EN） | 保留 | 无 | engineering.md「验证」 |
| F9 优先用真实实现；必须隔离外部依赖时用最小替身；模拟环境的 PASS 不能代替目标行为的证据（EN） | 保留 | 无 | engineering.md「验证」 |
| F10 命令、工作目录、环境、输入、退出码、原始输出要足以复跑（EN） | 脚本承担 | record 的结构（design §23.1） | — |
| F11 要检查目标确实运行了：空输入、零目标、静态扫描或退出码 0 都不能单独证明验收（EN；RV） | 保留 | 无 | engineering.md「证据」 |
| F12 日志和报错由工具原样导出，模型不改写、不重排证据（EN） | 脚本承担 | record 直接捕获 stdout/stderr | engineering.md 一句 |
| F13 必需验证没运行就不能算通过；外围项允许跳过时写 SKIP/NOT_RUN 和原因（EN） | 改写 | 交付里有“未运行/失败”行，但 gate 不对照验收逐项检查 | engineering.md「证据」 |
| F14 失败的命令也会留记录，不能只看记录文件存在就判断通过（EN） | 缺口 | gate 接受 exit_code≠0 的 record 作为 VERIFIED/RELEASE 的唯一证据 | 脚本检查：末节第 6 条 |
| F15 代码证据：开发根、固定提交或工作区差异；基线和其他批次的改动不算本批产出（EN） | 改写 | `snapshot`；交付的“改动与版本”是自由文本 | engineering.md「版本」+ 末节第 8 条 |
| F16 结论要写清哪些验收已证明、哪些没运行或受阻、哪些待决定（EN） | 改写 | 交付行没有按验收逐条对应 | engineering.md：“验证证据”一行写成“验收项 → 证据路径” |
| F17 纯设计不虚构实现验证；不为材料格式重复运行；已有充分结果不再 record（EN） | 保留 | 无 | engineering.md |
| F18 Git 引用不证明工作区与提交一致；源码不在 Git 里时不强迫建仓（EN） | 改写 | `snapshot` 只支持 Git 仓库 | engineering.md 一句（非 Git 时用 record 导出差异） |
| F19 证据文件要固定，不能用还在增长的日志；历史引用不得覆盖（EN） | 脚本承担 | run 里存证据哈希；哈希不符就挡住依赖方（routing._broken_orders，I5） | — |
| F20 角色不维护哈希；路径解析规则；`--data @file`（EN） | 删除 | 机制细节，已由 bf 处理 | — |
| F21 源码改变时更新 subject；审阅结果固定在被审版本，不会自动绑定到新版本（EN；PP） | 缺口 | 版本只是 200 字的自由文本；审阅通过后再开 implement 或 repair 不会让旧审阅失效 | 见 G19 和末节第 4 条 |
| F22 已有的失败、重试和历史证据不因精简而删除（EN） | 脚本承担 | 只追加（I9） | — |
| F23 计时和模型来源取自原始调用，未知不猜（EN） | 脚本承担 | run 记录 tool、model、session 和时间 | — |
| F24 记录数量、日志字数、测试数量、文档篇幅都不是进展指标（EN） | 保留 | 无 | engineering.md 末句 |
| F25 同一内容只维护一处；不建手工散列表、日报、风险报告、重复台账（AR） | 保留 | 无 | engineering.md 末句 |
| F26 参考 OpenCode Style Guide/Testing，不引入它的项目专属命令（EN） | 保留 | 无 | engineering.md 链接 |

## 7. 审阅者

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| G1 按本节点目标和契约确定检查观点，不机械套用全部风险类别（RV） | 缺口 | 无 | roles.md 审阅者第一条 |
| G2 每个新审阅节点首审用全新上下文；计划、决定、实现的作者会话不得审（RV；CX） | 脚本承担 | review 首审开新会话，排除作者会话（context.author_sessions，I4） | — |
| G3 输入固定版本、规格/验收、实际差异和原始证据；不传作者对话或期望结论（RV；PR §4） | 脚本承担 | 审阅卡列出版本、证据和验收（cards._body review） | — |
| G4 同节点 R2 可以续接 R1；会话成为作者或范围显著变化时新建；不重置预算（RV；PR §5） | 脚本承担 | review-resume，排除作者会话（context.py）；额度只增不减 | — |
| G5 证据先核对再采信：记录要能看出目标确实运行了；没有记录或只有自述的验收项不能放行（RV） | 改写 | gate 过滤掉非 record 文件；v2 审阅者只有 RELEASE 和 FIX_REQUIRED 两种结论 | roles.md：缺证据就判 FIX_REQUIRED，关闭条件写“补齐某项 record” |
| G6 对本节点关键验收至少亲自复跑一项；代价过高或环境不允许时说明原因，并核对原始记录和产物（RV） | 改写 | 卡片写“独立复跑”；gate 只要求 RELEASE 引用某个 record，引用执行者自己的 record 也能过 | roles.md + 末节第 7 条 |
| G7 只读审阅的留证：测试实际成功但 record 落不了盘时，不能因此判 WAIT_EVIDENCE；不为复跑扩大写权限（RV；PD） | 缺口 | 审阅用 `--access read`（Codex 是 read-only 沙箱），`bf record` 可能写不进 records/，gate 又要求 RELEASE 引用 records/。这正是 DEC-21/23 的老问题（需在真实后端确认） | 脚本：只给审阅开放 records/ 的写权限，或由驱动器代跑；见末节第 7 条 |
| G8 审阅不改待审产物（RV；TP） | 脚本承担 | read 访问模式（cli-adapter.md 说明 read 不是 OS 级隔离） | 末节第 7 条：审阅的 run 有文件改动就判不合法 |
| G9 只报真实阻断和最小关闭条件；非阻断建议不扣留 RELEASE（RV；PR §4；TP） | 脚本承担 | 阻断行必须有关闭条件（gate.FINDING）；建议单独写 | roles.md 已有 |
| G10 阻断必须对应本批目标、契约或验收，并给出实际可达的失败和影响；证明不了的列为建议；不要求执行者反证全部假想极限；不以代码风格阻断（EN 审阅边界） | 缺口 | 无 | roles.md 审阅者「什么算阻断」 |
| G11 集成批次只按约定的集成目标放行，其他功能成熟度另列后续；明确的安全、性能、资源契约仍可阻断（EN；RV；PR §4） | 缺口 | 无（审阅组也没有 focus 字段，见 B8） | roles.md 审阅者「什么算阻断」 |
| G12 普通缺陷按归属返修并用原反例复核；严重阻断转诊断（RV） | 改写 | “方案级:是”的阻断项直接交负责人兜底；审阅者没有直接开 decide 的出口 | roles.md：方案级的含义（要改方案或契约才能关） |
| G13 默认实现后审阅；只有明确的前置问题才先审设计（RV） | 改写 | 同 B20 | plan.md |
| G14 R2 核对原问题是否关闭和直接回归，不凭旧结论；发现目标内的新阻断仍要登记，但不全量扩大范围（EN；RV；PR §5） | 改写 | 复审卡写“只核对列出的阻断项与回归”，没说新阻断能不能报 | roles.md 审阅者「复审」 |
| G15 常规 RELEASE 不能代替第三方（EN） | 脚本承担 | 两个审阅组各自开 review，release 两个都等 | — |
| G16 返修关闭第三方问题要有权限，并基于当前版本的证据（EN can_close） | 脚本承担 | 返修卡挂在提出问题的那张 review 上，由同一 review 复审 | — |
| G17 finish 登记固定版本和原始证据（RV） | 改写 | “固定版本”行是自由文本，gate 不检查 | roles.md + 末节第 8 条 |
| G18 审阅结论是真实的四态：RELEASE/FIX_REQUIRED/WAIT_EVIDENCE/ARCH_DECISION_REQUIRED（TP；PR §8） | 改写 | v2 只有两态；环境坏了或依据矛盾时审阅者没有合法出口，只能续跑到用尽再转 decide | roles.md：缺证据 → FIX_REQUIRED；依据矛盾 → 方案级阻断。可选：允许审阅者用“外部阻塞” |
| G19 修了代码要更新版本和必要验证，不能拿旧审阅冒充新版本通过（EN） | 缺口 | 审阅通过后，decide、triage merge 或用户再开的 implement 只加入“仍开立”的审阅（routing.link），所以 release 会放行一个没人审过的改动；triage 排在全部审阅之后，它 merge 出来的 implement 必然没人审 | 脚本检查：末节第 4 条 |

## 8. 第三方

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| H1 只在计划节点做独立审阅（TP；PR §4） | 脚本承担 | review_groups 的 `reviewer: third_party` | — |
| H2 全新会话，不继承负责人（审阅职责）或作者的对话（TP；CX） | 脚本承担 | 审阅工单首审开新会话，排除作者（context.py） | — |
| H3 不输入常规审阅的结论（PP 合并审阅） | 脚本承担 | 审阅卡只放版本和证据，不放其他审阅组的结论（cards._body） | — |
| H4 核查代码、构建测试、规格和验收（TP） | 保留 | 与常规审阅相同 | roles.md 审阅者（第三方共用） |
| H5 不替执行者修复（TP） | 脚本承担 | read 访问 + 末节第 7 条 | — |
| H6 给出真实结论和最小关闭条件；非阻断事项不扣留放行（TP） | 脚本承担 | 同 G9 | — |
| H7 返修后只关闭本节点权限范围内的问题（TP） | 脚本承担 | 同 G16 | — |
| H8 缺固定版本或必需原始材料时说明缺什么，不拿常规审阅通过代替本次结论（TP） | 改写 | 无 | roles.md 第三方一句（判 FIX_REQUIRED 并写明缺项） |
| H9 第三方审阅的是已经审过的稳定版本（PP：可依赖常规通过来控制节奏） | 缺口 | 同 B7：第三方可能在常规返修之前审了旧版本，之后的修复不会触发第三方重审 | 随末节第 4 条一起解决 |

## 9. 上下文与会话

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| I1 相同模型或同一负责人不等于独立上下文；独立性来自会话和输入（CX；PP） | 改写 | v2 用 session ID 判断（context.author_sessions） | roles.md 开头一句 |
| I2 初次规划用新会话，并登记作者（CX） | 删除 | 计划由负责人在 bf 之外编写，没有计划审阅节点 | — |
| I3 计划作者会话不能审该计划（CX） | 删除 | 同上 | — |
| I4 同技术组的后续裁决续接该组的决策会话；无关组新建（CX） | 脚本承担 | decide 按交付目标续接（context.session_for） | — |
| I5 新的审阅节点首审用全新会话；同节点 R2 可续接（CX） | 脚本承担 | 同 G2/G4 | — |
| I6 同批执行者返修可以续接本批执行会话（CX） | 删除 | v2 有意让 repair 用新会话，避免锚定旧方案（design §9） | — |
| I7 上下文太长（300KB）时新建，记录原因（CX） | 脚本承担 | `policy.context.max_session_kb`，size-limit 规则 | — |
| I8 跨批执行、换备选、负责人兜底默认用全新会话，用证据文件交接，不复制完整对话（CX） | 脚本承担 | stage-change 规则；卡片由工单渲染 | — |
| I9 新建就是不 resume/fork、不粘贴作者对话（CX） | 脚本承担 | 适配器只按 `--resume-from` 续接；卡片不含对话 | — |
| I10 续接要用明确的会话 ID，不用 `--continue`/`--last` 去猜；Codex 用原生 UUID（CX） | 脚本承担 | agent_cli 的 resume-from 核对后端、会话 ID 和 cwd（cli-adapter.md） | — |
| I11 重建会话不增加技术轮次（CX） | 脚本承担 | 额度按 run 计，与会话无关（policy.used） | — |
| I12 编排者可以续接协调会话，每次都重读正式状态（CX） | 删除 | 没有编排者；前端不调度 | — |
| I13 首次执行给完整指令；续接只补差异；换执行者或兜底时给当前完整指令（PR §1） | 脚本承担 | 每张卡都完整渲染；同一 run 内续跑用 continue_prompt 只补差异 | — |
| I14 每个原始指令在卡片里只出现一次；当前节点放前面，不递归展开依赖链（PR §1；PP） | 脚本承担 | cards.render 的五节固定结构 | — |
| I15 工具或模型变了就开新会话，不跨后端续接（PP set_models） | 脚本承担 | tool-change 规则（design §21） | — |

## 10. 用户介入与授权

| v1 规则 | 分类 | v2 现状 | 建议去处 |
|---|---|---|---|
| J1 用户停止优先（SK；TL；OR） | 脚本承担 | `stop` / `stop --now`（design §23.4） | — |
| J2 技能本身和 DONE 状态不授权提交、部署、发布（SK） | 保留 | v2 SKILL.md 硬规则已有 | 维持现状 |
| J3 授权范围用用户原话登记（authorize），不推定适用于其他任务（SK；PD） | 脚本承担 | `envelope.batches` + approval 文件，按任务目录隔离 | — |
| J4 用户待决的问题由技术负责人发起，编排者原文转述，不补选项，不把技术核验推给用户（OR；PD） | 改写 | ask_user 只来自 decide 或用尽（register、routing.open_ask）；前端提示词没写“原文转述、不补选项” | 前端提示词（front.prompt）加一句 |
| J5 不能用其他角色的确认或空条件冒充用户授权；只是讨论、沉默或没回复都不算授权（PD） | 改写 | `answer` 只能由前端用原话文件登记 | 前端提示词：“只有用户明确的话才写进 answers/” |
| J6 临时 workaround 授权：在声明的任务、阶段、期限内有效，撤回或条件变化时重新核对（PD） | 改写 | envelope 就是默认的自主授权（design §7） | technical-lead.md「workaround」：envelope 内允许小修，边界见 D9/D10 |
| J7 用户离开模式（away/present）（PD） | 删除 | design §7：自主推进是默认，envelope 就是授权边界 | — |
| J8 预算批准要有结构化的数额，不能把“知悉风险”这类普通确认当成预算批准（PD） | 脚本承担 | grant 是结构化动作，必须经用户 approve 或 `bf grant --approval` | — |
| J9 先建议、后用户批准，只登记一处（PD） | 脚本承担 | ask_user 的 pending 动作在 approve 时执行（register.answer） | — |
| J10 涉及用户指定的约束、预算追加或验收承诺变化时，先登记用户决定，再应用变更（PD） | 脚本承担 | plan_revision 和 grant 转 ask_user；`revise` 要原话 | — |
| J11 用户返回时或最迟在最终验收前，汇总每项 workaround：原因、改动、验证/审阅、投入、临时措施是否已移除、残余风险、后续责任；不把延期汇报当成用户已接受（PD 用户离开） | 缺口 | report 只列在途、下一步、待决定和额度；decide 的诊断不汇总 | `report` 增加“已执行的裁决/临时措施”一节；technical-lead.md 要求在 diagnosis 里标“临时措施” |
| J12 保留方案涉及新承诺或风险接受时，要用户决定才能放行（PD） | 改写 | 无对应机制；负责人可以用 ask_user | technical-lead.md「向用户提问」 |
| J13 每 10 分钟汇报在途情况，不能静默停报（PD；OR） | 脚本承担 | 前端 CronCreate + `watch` 推送（design §23.3） | — |
| J14 用户明确指定模型时直接登记，不另开架构审批（PP set_models） | 脚本承担 | `set-role --approval` | — |
| J15 用户确认复用原始消息，不在设计书里手抄审批台账（AR） | 脚本承担 | approval 文件和哈希存入计划 | — |
| J16 用户待办只阻塞依赖它的路径，其余照常推进（PD；RC） | 脚本承担 | ask_user 只挡依赖方（design §5.4） | — |

## 建议新增的机械检查

1. **计划校验：每个交付目标至少属于一个审阅组。** plan.md 已经这样要求，但没人检查；漏配就等于没有独立审阅（A6、B5）。
2. **计划校验：instruction 和 acceptance 不能为空；有 `plan.design` 时，每个交付目标要有 `design_ref`，设计文件在 init/revise 时必须存在且哈希一致。** 替代 v1 的编排者人工核对，挡住“只写 ID”的指令（B12、B13）。
3. **设计漂移检查：每次派发前比对 `plan.design.sha256`（最好细到 design_ref 指向的章节），不一致就暂停受影响的交付目标，并在 report/watch 里提示。** 现在这个字段存了却从不检查，给人一种虚假的保护感（C15）。
4. **审阅失效重开：某个审阅组已经 RELEASE 后，组内交付目标又开了 implement 或 repair（来自 decide、triage merge、用户），就为该组新开一张 review，并让 release 等它；第三方组同样处理。** 否则未经审阅的改动会被放行，triage merge 的情况必然发生（G19、B7、H9）。
5. **decide 的 `supersede` 指向 review、release，或指向有依赖方的工单时，视为超出包络，转给用户。** 现在负责人可以在包络内作废审阅，让下游直接满足（D13、D17）。
6. **门禁：VERIFIED/RELEASE 引用的 record 中至少有一份 exit_code=0，并且是在本 run 开始之后生成的。** 防止拿失败的命令或旧记录充当通过证据（F14）。
7. **审阅的独立复跑与只读：RELEASE 必须至少有一份在本次审阅 run 内生成的 record，否则要写明“未复跑原因”行；审阅 run 若有文件改动（gate.scan 已经记录 `edited`）就判不合法；适配器为审阅只开放 records/ 的写权限，或由驱动器代跑 record。** 现在审阅可以只引用执行者的记录，而在只读沙箱里又可能根本写不了 record（G6、G7、G8）。
8. **版本绑定：“改动与版本”“固定版本”必须是提交哈希或 snapshot 记录路径；审阅的固定版本要等于该交付目标最近一次 implement/repair 的版本。** 让 I5 的“绑定固定版本”真正落地（F15、F21、G17）。
9. **路由：审阅的“非阻断建议”像“候选新项”一样进入本批 triage。** 否则这些建议直接丢失，v1 的“收口前确认去向”无从落实（D21）。
10. **（可选）计划校验警告：`rounds.review` 小于 1 + 返修链长度时给出警告。** 让负责人在审计划时就和用户说清楚，而不是运行中频繁 grant（B17）。

## 建议的 v2 文档结构

| 文件 | 用途 |
|---|---|
| `SKILL.md`（现有） | 入口、组成、对外接口、硬规则；补上“第一证据是代码和实际通过的验证”（F1） |
| `references/plan.md`（现有，增补） | 只讲 plan.json：字段、校验、instruction 五行模板、审阅组 `focus`、roles 的模型来源和解析（A13、A14、B8、B13、B20） |
| `references/technical-lead.md`（新） | 负责人的全部规范：开工与分流、批次与审阅节奏、计划提交与修订、裁决（先诊断、workaround、向用户提问）、分诊、兜底（A、B、D 三节的保留/改写/缺口项） |
| `references/design-book.md`（新） | 设计书的位置、结构、模板、写法、自查、更新约束（第 3 节；基本可以照搬 v1 artifacts.md 的设计书部分并去掉台账术语） |
| `references/engineering.md`（新） | 三类角色共用的复杂度、验证和证据标准（第 6 节）；卡片“约束”节固定附上它的路径，执行者和审阅者需要时再读 |
| `references/roles.md`（现有，增补） | 执行者、审阅者（什么算阻断、复审范围、缺证据怎么判）、第三方的要点（第 5、7、8 节） |
| `references/operations.md`、`references/cli-adapter.md`（现有） | 不变；前端提示词补 J4/J5 两句 |

合计新增 3 个文件。v1 的 protocol-*.md、orchestrator.md、contexts.md、prompts.md、hosts 文档不恢复：其中的角色规范已在上表拆到这几个文件，其余是脚本已经承担的机制。
