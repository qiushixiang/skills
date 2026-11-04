# 执行者接管与技术预算

主执行→备选→负责人兜底的接管配置，以及预算记账、环境失败与续作。字段总览与存储规则见[协议总览](protocol.md)。

## 执行者接管

新计划为executor的design/execute任务预置handoff，主执行者在原consumes预算内工作，主轮次仍不足时自动选择备选一次，再不足则技术负责人兜底。字段如下，不新增角色或独立接管台账：

```json
"handoff": {
  "backup": {"tool": "<已确认工具>", "model": "<已确认备选模型>", "effort": "<已确认effort>"},
  "backup_budget": "<限额为1的预算ID>",
  "lead_budget": "<有限兜底预算ID>"
}
```

backup可附principal用于标识备选执行者。backup_budget、lead_budget必须存在且互异，不能被任何任务consumes作为主/审阅预算使用，也不能在不同任务混用为备选与兜底两种阶段；备选限1技术轮次，负责人限额由计划明确（通常先预留1轮），不得无限尝试。三段预算是事先分配的不同额度，不重置或增加原预算；同一问题拆分、合并或设计/实施共用一次备用机会时复用相同预算ID，不新建同义预算绕过一次上限。已有任务无handoff兼容原有裁决路径；引入配置须经plan_update。

status在合法实现动作中输出executor_stage=primary/backup/lead、实际责任role、call_config和该段remaining。主执行预算耗尽且仍需解决时选择backup，备选交付失败或后续审阅仍要求返修时选择lead；正常成功且无返修不激活接管。lead的role为architect，使用roles.architect实际配置；主/备均为executor，备用实际配置来自handoff.backup。共享审阅和既有审阅预算保持计划约定，应覆盖必要返修，不因接管机械新增审阅节点。

start由任务dispatch_owner登记（旧任务默认orchestrator，局部任务显式architect，快捷命令需 `--actor architect`），call仍须匹配status指定配置；阶段切换用全新实现会话，session_policy=resume时通过--data补replacement_reason。新会话未知仍可用pending占位，finish补实际ID时再次检查没有复用前任上下文。备选的一次指技术解决机会；INVALID工具重试沿同一stage/retry_of受限重试，不重复扣备选技术额度，重试耗尽仍为工具阻塞，不能无限循环。

生成的attempt保存executor_stage、performer_role、performer_config及实际consumes，technical_round继续累计。finish按该attempt的performer_role登记（lead为architect），实际配置按该attempt快照核对；finish.subject_versions仍需绑定真实验证版本。metrics在既有attempt明细显示阶段与实际责任，不另建报告。接管激活后不能换handoff配置/主预算ID重领机会；预算限额调整仍须用户依据，不能使阶段回退。

负责人兜底是实际实施，不能只作一句诊断或再次派回原执行者；交付仍经固定版本的独立审阅。实现会话不得自审，架构决定不代替RELEASE。兜底用完仍未解决时提示预算裁决；合同冲突、授权变化或预算追加继续沿既有裁决规则，不能为了接管自行改验收。交接只引用源码、已有验证和未关闭问题，不增加材料。

## 审阅预算配套：脚本门禁

预算授权可来自 resolve 时已有的用户证据，或后来登记的同裁决、有效且范围匹配的用户 budget_approval 确认；无需同步两份 user_evidence。登记和旧确认补标见[裁决授权](protocol-decisions.md#先建议后用户批准单处登记)。

`flow.py` 对 init 和 plan_update 强制执行 full_path 配套校验，不存在靠提示词豁免的开关。单个实施/设计任务可交付的技术轮次上界是主预算（多个 consumes 同时扣费时取最小限额）加备用、负责人额度；对应独立审阅须覆盖每次可能交付。例如主执行2＋备用1＋负责人1，需要审阅4；主执行3＋备用1＋负责人1，需要审阅5。备用额度专属备用执行者，不是原执行者加轮，也不包含审阅。

联合审阅按先交付所有成员、随后逐项返修的保守上界预留：`1 + 主轮次总上界 - 首次交付任务数 + 去重后的备用/兜底额度`。完全相同的共享主预算池只算一次，池内上界取最小限额；同一组的共享备用/兜底预算只算一次。部分重叠的主预算池可能高估，脚本采用保守上界，不声称求得最小配额。多个独立审阅节点共用同一预算时，各节点需求相加；同一轮不能替代多个独立节点的审阅。

关联来源：review/third_party/verify 的 repair_tasks 指向 design/execute 时必须配套；普通 review 还检查直接依赖的 design/execute。普通/第三方审阅未配置 repair_tasks 时沿依赖寻找最近的 design/execute，不能靠删掉 repair_tasks 绕过预算检查（这不自动补齐返修归属）。无关联实施来源的有预算审阅节点至少预留首审1轮。handoff 任务必须存在上述关联的独立审阅节点；不得把实施和审阅混在同一预算池规避配套要求。非有限实施来源、缺审阅预算或配额不足均拒绝新计划登记，并列出缺口。

status 返回 `review_budget_coverage`，列出每个审阅的来源、所需上界、预算总需求及错误；validate 发现缺口返回 ok=false 和退出码2。旧台账仍可读取、回收在途结果及登记诊断，不自动追加额度或重置消耗。对尚待派发的实施/返修，相关配套不足或审阅额度耗尽时返回 WAIT_REVIEW_BUDGET＋PLAN_REPAIR，start 因非 READY 被拒绝：先修计划和授权，再消耗主执行、备用或负责人额度。不会中断已在运行的代理。

已有效放行批次的未修改任务不要求补加历史预算，但它们已消耗的共享审阅额度仍计入总池需求；修改或重新失效后重新检查。新增预算池、替换既有 consumes/备用/兜底预算ID也要求引用用户决定，不能通过换ID绕过预算追加授权。此校验保证计划内声明路径的配套，不自动证明未声明的业务影响、源码版本更新或审阅语义正确。

## 技术预算记账与续作

每次调用保留独立attempt及实际投入；技术预算按同一技术轮次的根尝试去重，不按调用数相加。默认仍按现有规则预占技术额度，不因结果为INVALID/WAIT_EVIDENCE自动免扣。`retry_of`与获批的`continuation_of`均沿原根轮次；新的有效工作仍使该根占用一次额度，不能因根调用曾为环境失败而免掉后续实质工作的消耗。所有计时、原result/subjects/consumes/technical_round及证据保持原记录；有效预算和分类由追加记录派生。

architect核对原始交付、实际差异、工具及验证后调用`classify_attempt`。必需非空reason/evidence，work_performed为显式布尔判断；程序校验结构、角色及关联，不声称能自动证明“没有实质工作”。每个已结束attempt至多分类一次，存入state.attempt_accounting，附actor、时间、revision和依据哈希：

| kind | 适用条件 | 预算与后续 |
|---|---|---|
| environment | 尚未开展实质设计/实现/验证，仅启动权限、工具或网络能力前检失败；work_performed=false，结果INVALID或WAIT_EVIDENCE，不能隐藏已登记对象版本变化 | 本调用免扣；最新任务进入WAIT_ENVIRONMENT，先修复实际执行环境再获准重试 |
| registration | 只修正登记或版本关联，未新增设计、实现或验收验证；work_performed=false，结果为任务成功类型；source_attempt须指向本任务更早的成功交付，且保留共同的同哈希原交付证据 | 仅免扣本次登记修正，原交付的技术消耗保持；不使旧审阅自动覆盖新版本 |
| withdrawn | 未派发的冗余 start：work_performed=false、result 仍为空、会话为 pending，且没有 dispatch/call_id/timing/retry/continuation；必须是本任务最新尝试，source_attempt 必须是紧邻的更早成功交付，版本匹配且全部证据完好；附真实执行前拒绝/未启动依据 | 只撤销未发生工作的预留，不删除或改写原 attempt；latest 回到前一交付，未来不能给该占位补派发或交付。不能撤销已运行调用、技术失败或损坏的原交付 |
| decision_pause | executor的design/execute在约定边界暂停待裁决，已有实质进展；work_performed=true，结果WAIT_EVIDENCE或ARCH_DECISION_REQUIRED；decision_id须关联本任务的blocks、恢复任务或已登记triggers | 已用技术额度保留；进入WAIT_RESUMPTION，决定落实后可批准原轮次续作 |

实质返工、重做、方案改变或技术失败仍需新技术轮次；FAILED/FIX_REQUIRED不能分类成环境失败或裁决暂停。实现已完成、最后网络验证受阻的调用仍计技术投入；只有后续单独的启动前检查失败可按其自身事实免扣。历史纠偏也逐项通过classify_attempt，不修改原attempt或整批清零；不自动迁移任何旧台账。若原根已有有效工作后继，仅给根调用免扣不会退还后继占用的同一技术额度。

`resume_attempt`只接受最新已分类的environment/decision_pause尝试；附新的恢复证据，不能原样复用失败调用的整组证据。环境证据须来自实际将运行任务的上下文，明确所需文件/命令许可或DNS/SSH能力及启动配置；编排者可联网不是执行者可联网的依据。architect据此确认条件确有变化，未解决的同类错误不得原样重派。

裁决续作须先有resolution：本任务被blocks阻断时等待决定闭环；本任务自身属于决定的恢复工作时，只检查该任务恢复条件的前置依赖已满足，不要求先关闭依赖其完成的整个决定。其他关联情形要求决定已闭环。所有其他授权、依赖、恢复版本及审阅检查照常生效。恢复依据哈希变化或关联裁决前置重新失效时，未使用的许可不再有效，不能凭旧许可开工。

许可追加在state.resumptions，保留actor、时间、revision、原因和证据哈希；每个源attempt仅一份有效许可，有效时重复批准会被拒绝。旧许可失效后补发须新依据，旧记录仍保留且再次占用许可次数。task.environment_retry_limit默认1、continuation_limit默认2，均为每任务累计许可次数（不是每次新上下文重新计算），非负整数；可在初始计划明确，已有任务扩大上限须plan_update引用用户决定。额度耗尽、依据失效或仍无合法恢复条件时停止并交技术负责人判断，不自动重发许可或加技术预算。

批准后重读status，start原样使用动作给出的continuation_of并核对call_config。续作沿原接管stage与预算ID，保持上下文隔离；根轮次已计费时不新增扣费，环境失败后的实际有效工作则正常占用原预算的一次机会。新attempt记录continuation_of及相同technical_round，旧attempt不动。续作后真正失败，再按剩余技术预算和既有接管规则决定下一轮，不因续作本身换人。

快捷入口示意（复杂参数经--data，所有写入仍需revision/CAS）：

```text
flow.py classify_attempt --actor architect --revision <REV> --data '{"attempt":"A0005","kind":"environment","work_performed":false,"reason":"启动前网络权限失败，未开展实质工作","evidence":["<原始失败记录>"]}'
flow.py resume_attempt --actor architect --revision <NEW_REV> --data '{"attempt":"A0005","reason":"已在实际执行环境验证网络与权限恢复","evidence":["<新的只读能力验证记录>"]}'
```

上述操作既不提升预算上限，也不放行任务；finish仍按实际结果登记。技术预算、环境许可次数与完整调用/耗时分别见metrics，不将免扣误报成没有投入。只读查询不得为了适配新规则写入分类或更改历史。

withdrawn 的用途仅为撤回可证实未派发的冗余占位；其余已有交付、版本、独立审阅和裁决条件仍重新核查，不能借它认可新产物或修复失效证据。metrics 单列 withdrawn_starts，不计模型调用或技术轮次；原占位仍在 attempts 明细中，model_source 标为 planned_unlaunched。例如 `classify_attempt --actor architect --data '{"attempt":"A0027","kind":"withdrawn","work_performed":false,"source_attempt":"A0024","reason":"执行前拒绝，未派发；原交付已恢复且证据完好","evidence":["<原始拒绝及核验记录>"]}'`，仍需正常 revision/CAS。
