# 阻塞、裁决与恢复

严重阻塞诊断、临时 workaround 授权、用户离开状态与 resolve 恢复条件。字段总览与存储规则见[协议总览](protocol.md)。

## 严重阻塞与临时workaround授权

执行者在交付中报告失败证据、已做工作、影响和最小缺项并暂停受影响路径，由派发责任人代登记signal；编排者向用户告知当前状态并派本任务技术负责人诊断，不能把SEVERE_BLOCK标签本身当成“必须让用户选方案”的结论。负责人先核对是实现/环境问题、方案缺陷还是授权/范围确需变化，避免未诊断就追加预算或将技术问题转给用户。

用户待决问题由技术负责人发起：先登记 resolve 中的 confirmation owner=user，再由 next 原文转述。历史 decision point 标为 owner=user 时仍先派负责人整理正式问题；无用户原始答复时，只能登记尚待确认的用户条件，不能用其他角色确认或空条件当作用户已授权。已存在的编排者机械放行节点沿正式依赖执行，不因这条发问规则增加技术裁决。

用户可在任务执行中临时授权编排者申请负责人判断，并允许在约定边界内以小代价workaround修复。以用户原始消息为准，授权在声明的任务/阶段/期限内持续有效，明确撤回或条件变化时重核；不能推定适用于其他任务。复用既有消息/调用记录或必要原文文件，不另建授权表。参考表达（具体边界依用户原话，不要求机械补齐数字）：

```text
本任务遇到阻塞时，允许编排者先交技术负责人判断小代价workaround。
在已授权范围和剩余预算内，可实施可撤销、可验证的局部修复，记录原因、代价、验证及后续去向，优先恢复被阻塞批次，无需再次向我确认同一授权。
技术负责人确认确实无法用上述方案恢复时，保留该批次的阻塞和需要我决定的事项，再继续当前已授权、且不依赖该阻塞的其他批次。
超出此边界的方案先给我具体建议，不能自行降低验收或追加预算。
```

**推进顺序：先尝试恢复被阻塞批次，确实不可行才转做其他批次。** 负责人先提出本批局部修复方案，说明为什么能解决阻塞、涉及的对象/路径、预期投入及停止条件、兼容/临时影响、可撤销办法与必要验证。有直接证据证明方案不成立时可直接否决，不为满足流程强行实施一次。可行且已获授权时，沿现有责任、预算及门禁派执行者实现并验证，必要时按计划更新受影响版本和独立审阅；不能只批准“workaround”文字就将批次放行。

“小代价”不是无限修复许可。负责人把用户描述落实为本次有界方案，不能擅自追加额度、放宽验收、改变公共契约或跨越用户未允许的修改边界。用户可明确临时调整原范围，已覆盖具体行动时沿用该授权，通过正式计划/管理操作落实，不因旧默认约束重复索取同一许可。若用户只允许技术判断，不能据此推定允许超出既有授权的实施；但已有实施授权本来覆盖的局部修复仍可推进。需要用户新增决定时，提供已诊断问题、推荐方案及代价/影响，并用user confirmation保留真正的待决项。

负责人确实判定本批小代价修复不可行或已触达停止条件时，记录依据与剩余最小缺项，保持本批未通过及相关依赖受阻；编排者再选择其他满足依赖、门禁和执行授权的READY任务。不得把无关任务加入blocks，也不得跳过依赖使下游虚假READY。转做其他批次是本批恢复不可行后的推进安排，不代替技术诊断，不关闭原阻塞。原已获准的独立在途工作无需因诊断全局停机，但不能用“其他任务在跑”省略对被阻塞批次的优先恢复判断。

负责人用resolve.summary记诊断、采用或否决的workaround及依据；已有临时授权作为user_evidence引用，恢复工作用既有task/confirmation条件表达，版本/实施/验证依旧按正式操作登记。不要在blocks里阻塞决定自身必需的恢复任务；修复与审阅真实完成后才能close。交付/问题记录说明临时方案的适用边界、残余限制、保留/移除条件和后续责任，不另起workaround文档套件。实际实现、返修及验证按技术预算计入，不能借临时方案免扣或重置预算。

## WAIT_EVIDENCE 诊断与重检

正式任务返回未分类的 WAIT_EVIDENCE 时，status 生成 `evidence:<attempt ID>` 问题，包括没有 consumes 的 confirmation 任务。派发责任人机械执行 `signal --from-issue <key>`，随后派 architect DECIDE；不凭等待结果请求用户，也不重复原任务。已明确分类的 environment / decision_pause 仍走[有限续作](protocol-budget.md#技术预算记账与续作)。同一次等待先登记一个诊断，不同时生成重复的证据与预算问题；诊断闭环不追加或重置预算，原额度耗尽仍受预算门禁限制。

只读审阅的等待先按[审阅留证例外](../workflows/review/SKILL.md)核验宿主保存的原始工具记录（CLI 为 run/stdout）：其中已有验证实际运行成功、仅 record 落盘失败的，按已运行采信，不得把审阅环境写 records、/tmp 或 `flow.py record` 成功列为恢复条件或前检门槛，也不为此扩大审阅写权限。测试本身因只读未能运行时，恢复条件只能选择只读环境可运行的代表性验证，或引用有写权限节点已留存的原始记录。

非任务 CONFIRM_CONDITION 缺证时，各宿主统一登记正式操作，不依赖卡片缓存：

```text
flow.py decision_progress --actor orchestrator --revision <REV> --data '{"id":"<原裁决ID>","status":"waiting","requirement":"<原确认条件ID>","evidence":["<原始等待报告路径>"]}'
```

脚本保存在可选 `state.confirmation_waits` 数组：原 decision / requirement、由原始证据哈希确定的 source_key、evidence_hashes、actor / at / revision；旧台账无此字段仍可读。只接受未满足且前置依赖已满足的 confirmation，不解析自然语言原因。非 user 条件生成 technical_decision，目标为原条件，blocks 沿用原裁决（允许为空，不扩大范围）；原确认暂不派发，直到对应 source_key 的诊断以结构化恢复条件实际满足并 close。空 blocks 的 architect 自有确认可由其登记等待、看到诊断，但不因此取得其他任务或闭环权限。仅显式 owner=user 的条件保留用户待办；依赖未完成不转成用户条件。

诊断不能以 clarification 空条件直接解封；负责人必须登记 task / confirmation 恢复条件及证据。原条件 confirmed 后等待失效，历史仍保留。无关任务完成、计划版本变化及 `--recheck` 都不能绕过未闭环诊断。已闭环的匹配诊断允许重新核对原条件；再次缺证用新的原始交付登记新等待。原始等待文件不可覆盖；机械 signal 复用并核验其已存哈希。

派发卡回收 CONFIRM_CONDITION / WAIT_EVIDENCE 自动调用上述操作；`next` 对旧版已结束等待卡按原始 result.md（报告缺失时保留原 state.json 等待回执）机械补登记，不改旧卡结果或猜测 user 所有权。`pre_yield` 只读时指出待登记操作，不将旧技术等待伪装成 WAIT_USER；不使用卡片的全局进度计数决定重检。

## 用户离开状态

“用户离开状态”是本任务内可撤销的临时推进委托，复用上一节的workaround权限边界。用户明确说“进入用户离开状态”即按这套已定义规则授权；如另附可改范围、代价、时限或禁区，以本次用户原话为准。编排者简短复述有效边界，用`set_user_mode`直接登记，不为启用/解除该模式再要求架构审批。只是讨论该功能、用户沉默或暂时未回复均不构成启用。无记录的旧任务默认present，不自动迁移为away；不同任务不互相继承状态。

```text
flow.py set_user_mode --actor orchestrator --revision <REV> --data '{"mode":"away","authorization":"本任务按用户离开规则，在现有范围与剩余预算内先诊断并以小代价、可撤销、可验证的workaround恢复被阻塞批次；确实不可行再推进其他可派批次；保留待决事项并统一汇报","user_evidence":["<用户启用原文路径>"]}'
flow.py set_user_mode --actor orchestrator --revision <NEW_REV> --data '{"mode":"present","user_evidence":["<用户解除原文路径>"]}'
```

字段mode只支持away/present，user_evidence非空，away另须非空authorization准确表达用户授权；present不允许附新的离开授权。脚本将最新状态、授权文字、原文哈希及登记人/时间保存于state.user_mode，history保留历次启用/解除；status输出当前模式。操作只更新状态与revision，不改plan_version、预算、模型、attempt、执行授权、裁决、确认或任务结果。它记录委托而不解析自然语言授权，不自动确认任何用户条件；编排者在每次新派发、扩大方案或接管时核对最新模式及原文限制。

离开期间按四步工作：

1. **优先技术诊断**：严重阻塞先交本任务专用architect会话，不立即把整个任务挂起等待用户手工处理。确有新增用户决定的事项登记并保留，不能以away代签。
2. **小代价恢复本批**：负责人判断并设计局部、可撤销、可验证的workaround，明确范围、代价/停止条件、临时影响和验证；在已有及本次授权内由责任执行者实现。记录复用现有决定、证据、差异和交付，不新增重复台账。
3. **连续推进**：workaround实际验证且约定审阅有效后，解封满足条件的本批及后续路径；不能只因方案“看起来可行”便放行。确实不能在授权内恢复本批时，再保留阻塞并推进其他当前可派、已授权且不依赖它的工作。提前构建准备/独立审阅只有在各自依赖和门禁确实满足时才能派发；不强行绕过阻塞。
4. **集中汇报核销**：用户返回时先同步实际进度；最迟在最终验收与发布前，汇总每项workaround的原因、改动与影响、实际验证/审阅、投入、临时措施是否已移除或拟保留、残余风险及后续责任。区分“已移除并验证”“拟保留且已在授权内验证”“仍需用户决定”，不把风险延期汇报当成用户已接受。已授权内无残余的小修汇报供知悉；保留方案涉及新承诺、风险接受或其他明确用户门禁时，仍取得相应决定才能放行。最终汇报本身不自动完成验收或授权发布。

用户说“解除用户离开状态/我回来了”时，编排者登记present并说明撤销的委托及当前在途项；不再依据away额外启动新修复。原任务已有明确执行授权继续有效，解除不等于取消全部任务；正在写入的工作先在安全边界交接，不能粗暴杀进程，也不能用“已经启动”无限延长已撤销的委托。已实施措施不自动回滚或从记录删除，后续保留/撤销按实际状态及用户意图处理。明确时限/结束条件由编排者依原文核对，脚本不猜测或自动延长。

离开不放宽预算、验收、版本有效性、独立审阅和宿主权限；超出有效授权的行动保持待决。用户可以明确附加更具体的授权，确实覆盖时直接沿用，不重复请示同一事项。原每10分钟在途汇报和定时器仍运行，除非用户明确改变通知频率；不因离开而静默停报或假装有离线运行能力。这里只定义模式及管理入口，维护技能不代表已替任何实际项目开启该状态。

## 裁决恢复条件

普通resolve.requirements 是非空数组，元素必有 id/kind/depends_on/instruction。支持：

- task：另填 target 和 versions；versions 必须完整等于该目标当前 subject 版本映射。
- confirmation：另填 owner（五类角色或 user）；表示程序无法判断的技术/用户条件，由当前派发责任人用 decision_progress 的 status=confirmed、requirement=条件ID及对应角色原始证据登记。

程序不解析 instruction。自然语言安排必须对应上述可检查条件；未知 kind 拒绝并保留原数据。条件ID在同一任务存储中应唯一；不能复用其他裁决的确认记录。

恢复任务交付或返修合法推进对象版本后，已有 task 条件可能仍钉住旧版，阻止后续恢复任务启动。此时仅 architect 可用 `repin_requirement` 显式重钉：`id` 是已有且含 resolution 的决定，`requirement` 是其中已有的 task 条件ID，`versions` 必须完整等于该目标当前 subject 版本映射，并且与原映射不同。不能重钉 confirmation 条件，不能借此增删条件、改 kind/target/depends_on/instruction/owner 或决定 blocks/status；任何上述字段作为操作参数都会被拒绝。决定若已闭环，重钉也不会自动改其存储状态，实际有效性仍由现有条件检查判断。

重钉须给非空 `evidence` 修改依据，并至少提供 `context` 或非空 `reason`；context 沿用架构上下文校验。操作走相同的 CAS、request_id 幂等、事务及 history 路径，快捷命令可用 `repin_requirement --revision <REV> --actor architect --data -`。决定的 `requirement_repins` 追加原条件和修改后条件的完整快照、actor、时间、revision、依据文件哈希以及所提供的 context/reason；原 resolve 历史不改写。同值请求拒绝且不新增记录；同一 request_id 的原请求重放仍按通用幂等规则不重复记录。

重钉不是任务交付或审阅通过，不改 attempts、预算消耗、用户确认记录、授权或计划。`success()` 与 `requirement_ok()` 的版本及证据判定保持原义：旧版本审阅不会因重钉变为新版本有效，任务证据失效仍不能满足条件。修改依据哈希用于审计，不替代恢复任务证据；仍须完成其余恢复条件并按既定 close 检查闭环。多个条件需要重钉时可在一个 apply 事务内逐项提交，任一不合法则整笔不写入。

首次交付不要求机械地把 v1 改成 v2；版本标识应对应真实交付及语义变化。若设计交付或审阅返修确实推进了版本（包括后续 v2→v3），技术负责人先核对受影响恢复条件及变更依据，再对仍需约束当前成果的 task 条件显式重钉；不得自动追随最新版或借重钉继承旧版审阅通过。重钉后重新查询状态，按原预算/接管和独立审阅规则继续。

以下为resolve操作输入的恢复条件示意，不是向decisions区粘贴的内容：

| 条件ID | kind / target | 绑定版本 | depends_on | instruction |
|---|---|---|---|---|
| DEC-01.design | task / B07.revise | B07.design为D2 | 无 | 修订D2设计 |
| DEC-01.review | task / B07.design-review | B07.design为D2 | DEC-01.design | 审阅D2所有权契约 |

resolve同时提供裁决id、summary、非空evidence和user_evidence等协议参数；程序校验后生成决定记录。

### 先建议、后用户批准：单处登记

resolve 时尚未获批，user_evidence 保持空；用户随后批准时，在对应 owner=user 的 confirmation 上一次登记证据和机器可读的 budget_approval，不回填或改写 resolution.user_evidence。例如：

```text
flow.py decision_progress --actor orchestrator --revision <REV> --data '{"id":"DEC-09","status":"confirmed","requirement":"DEC-09.user-budget-approval","evidence":["<用户原始批准文件>"],"budget_approval":{"limits":{"B01.R":5}}}'
```

budget_approval 只存于这一确认记录。limits 是预算ID到明确批准的目标限额；任务预算绑定或许可上限变更可另给 tasks，例如 `{"regular":{"consumes":["NEW_R"]}}`，支持 consumes、backup_budget、lead_budget、environment_retry_limit、continuation_limit。未列出的池/字段、不同目标值不获授权；不能将“知悉风险”等普通用户确认当成预算批准。脚本校验结构和证据，登记责任人仍须核实原始用户消息确实覆盖这些值。

plan_update 从同一 decision 下有效的用户 confirmation 读取授权：要求 decision 关联一致、owner=user、非空证据存在且哈希不变、该条件及其全部前置条件满足，且批准范围覆盖本次全部预算变更。可合并同一裁决的多个有效预算确认，不能挪用其他裁决的确认。原 resolve.user_evidence 的既有授权路径仍兼容，但也检查文件和哈希。plan_changes 只记授权来源字段/条件ID，不复制一份需同步维护的证据。

旧确认已经保存证据、但没有 budget_approval 时，使用 decision_progress 的 status=bind_budget_approval，传相同 id/requirement、原 evidence 和准确 budget_approval。只允许为同一条 owner=user 的既有确认补标一次，必须复用相同的完整原证据；保留 evidence 和裁决头不变，操作进入 history，不需要再申请同一用户授权。已有 budget_approval 不得覆盖或通过再次 confirmed 清除；新的授权范围使用新的用户条件/裁决登记。未标注的旧确认不会被程序按自然语言或条件名猜测为预算批准。

目标任务先由技术负责人通过 plan_update 配置，再 resolve 引用；可在同一原子请求中完成。pending 裁决可以作为计划修改依据，变更历史保留，不能通过撤销门禁冒充原门禁通过。涉及用户指定约束、预算追加或验收承诺变化时先登记用户决定，再应用变更。

恢复条件满足后用close一次闭环；内部检查条件并记录applied_at/closed_at，兼容旧的分别登记applied/closed。closed后仍重新检查整体任务门禁；其他裁决、缺第三方、预算或版本失效不会被本裁决清除。条件有依赖时，建议只提示依赖已满足的恢复任务。

resolve.dispatch 可选，是对任务下一次派发的一次性约束数组：每项 {task, fresh_session?, note?}，note 不超过 300 字。resolve 之后该任务首次生成派发卡时，fresh_session 强制新执行会话并自动写 replacement_reason，note 以“裁决要求（裁决ID，本轮）”进入卡的执行节；该任务再有 start 后即失效。持续有效的操作要求仍写进任务指令（plan_update），不要堆在 note 里。

纯澄清可用resolve.clarification=true，requirements=[]、user_evidence=[]，一次决定并关闭。仅architect所有、非budget_exhausted信号可用，且报告以来plan_version和subjects均未变化；需要后续修改/验证/确认时禁止使用。旧裁决没有报告版本记录时仍走显式恢复条件，不回填推断。澄清不替代实现、审阅、用户批准或其他门禁。


## 被新范围裁决替代的历史决定

只有技术负责人可执行 `supersede_decision`，参数为 id、replacement、reason、evidence、context。它用于旧恢复路径已被用户授权的新范围和正式计划停用后收口历史提示，不用于跳过仍有效的修复、测试、审阅或预算门禁。

前提：原决定尚未有效关闭；全部 blocks 已 retired，且没有活跃任务依赖它们；不同的替代决定已经 resolve，含完整有效的用户范围授权和裁决证据，且已据其正式 plan_update。原 summary、requirements、证据、用户确认与 attempts 均保留，新操作只追加替代关系、原因、证据哈希、作者上下文及登记时间，状态为 superseded。它不是 closed 或通过，不自动满足原条件、确认预算或返还消耗；新路径仍须完成自己的真实门禁。

status 不再为 superseded 决定生成旧预算/恢复提示。原任务仍保留原 blocker，不能以替代为由重启旧路径；后续不能用旧决定改计划、确认条件或重钉版本。若要重新开展该范围，须明确规划新的恢复路径。旧计划/原证据仍可审计。
