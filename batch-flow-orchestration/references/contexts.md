# 上下文分组与作者隔离

principal 表示同一技术负责人，architect/reviewer 是不同职责；相同模型或负责人不等于独立上下文。初次 plan 的作者 context 随 init 登记，实质变更随 plan_update，纯文档纠错随 design_refresh；决定作者随 decision_start/resolve，负责人兜底实现随实际 attempt 登记。新审阅节点不得使用这些作者会话，不能把 R1 会话转作后续决定作者。

初次规划把关联技术链路放同一 `context_groups`，无关域分组独立。技术决定在相应组的专用会话续接，同组新增问题只补当前差异；跨组契约由总体协调上下文处理。程序根据受阻任务所在组给 context_hint 并拒绝把已登记的决策会话跨组复用。旧 plan 无组字段按原会话方式继续，不自动迁移。

| 场景 | 上下文 |
|---|---|
| 初次规划 | 新架构会话，登记作者来源 |
| 同技术组后续裁决 | 续该组决策会话；失效重建保留旧新 ID、原因和预算 |
| 无关技术组裁决 | 新建，避免共享完整历史 |
| 新设计、计划、实现审阅节点首审 | 全新会话，不继承计划/决定/实现作者或其他节点 |
| 同节点 R2 | 范围未重定且会话未成为作者时可续 R1 |
| 第三方首审 | 全新独立会话 |
| 同批执行者返修 | 可续本批执行会话 |
| 跨批执行、换备选或负责人兜底 | 默认全新实现会话；证据文件交接，不复制完整对话 |
| 编排者持续推进 | 可续协调会话，每次重读正式状态 |

AGY 续接使用明确 `--conversation <ID>`，不凭 `--continue` 或 `--last` 猜测；宿主真实 ID 与模型需登记。新建意味着不使用 resume/fork 继承，不粘贴作者完整对话。程序可发现同 ID 复用，输入是否偷带旧对话仍由派发责任人核实。重建不增加技术轮次。计划审阅只在具体风险或既定门禁要求时配置，不是所有计划默认节点。

## 登记字段与程序检查

context 是 init/plan_update/design_refresh/resolve 等操作的参数：conversation_id（真实会话ID）、mode（fresh 或 resume）、reason；resume 时 source_conversation_id 必须等于 conversation_id，表示显式续原会话，不是复制另一角色的聊天。init/plan_update 的 context 记入 state.plan_changes，design_refresh 记入 design_corrections，resolve 的 context 留在裁决记录与 history；旧 store 缺作者字段时不追认。architect/reviewer 合并 principal 时，plan_update/resolve 必须提供 context。

程序检查：有计划修改作者记录的会话不能独立审阅该计划；start 用 pending 占位时，finish 补实际会话再查一次；需新建时保留旧尝试与预算并记 replacement_reason。status.actions 给 principal 和 context_hint（新审阅/第三方提示 fresh，同节点合规 R2 提示 resume，架构诊断提示续接本链路决策会话）。提示不启动代理，也不认证隐藏历史；宿主调用交接见[协作指南](agent-collaboration.md)。

Codex CLI 的 conversation_id 必须是其原生 UUID，逻辑组名不能代替。未知时 start/decision_start 使用 pending 占位，成功回收后登记原始回执中的 ID；新登记及成功结果禁止使用逻辑名称。适配器续接另需上一轮成功原始记录，见[固定入口](linux-cli.md)。status 只读查询遇到旧逻辑名时给 fresh 提示，不迁移或删除历史。

旧 pending 裁决若已错误登记非 UUID 的 Codex handling，会阻止正常回填新会话。可由 architect 显式再调用 decision_start，提供同一 id、合法新 call（原 tool/model/effort，conversation_id 为 pending 占位或真实 UUID）、非空 replacement_reason 和原始失败 evidence。脚本仅允许替换这类无效旧 Codex 会话，旧 handling 连同原因、证据哈希和时间追加到 handling_history；不改已完成裁决、预算、旧尝试和计划。合法 UUID 或 pending handling 不允许借此重开。随后正常 resolve 绑定真实结果 ID，不把旧逻辑名传给 resume。
