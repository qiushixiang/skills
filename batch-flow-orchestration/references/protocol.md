# 数据与变更协议（format_version=1）

## 正式台账与临时请求边界

本文说明管理接口字段，不是直接编辑flow.json的模板。正式台账及程序生成的状态、轮次、时间线和history仅由flow.py写入。角色不得用编辑器、补丁、shell重定向或自写脚本改正式台账；即使只改plan或修复格式也须走管理接口。

角色日常使用快捷命令，由脚本封装操作；复杂多操作事务可构造独立request.json并apply。临时请求的plan是init/plan_update的操作参数，不是允许直接编辑store.plan；context、timing和恢复条件也都只是相应操作的输入。程序负责结构/职责/CAS校验及正式记录生成。复杂plan/context/requirements等可通过--data传操作参数，不用构造请求信封。

## 存储和一致性

任务根默认 `.batch-flow/flow.json`；一个文件原子替换，逻辑分区 plan、state、decisions、history，外层 revision 是写入版本，plan_version 是计划修订编号。管理入口基于 fcntl 排他锁、expected_revision、request_id 幂等和临时文件+fsync+replace。目标环境为支持这些文件操作的 POSIX 本地文件系统。

查询不写数据。输出 actions 是建议，不是能力令牌；编排者仍可通过正常工具执行、复核或请求调整。role 是责任标签，不是认证机制。脚本验证字段与已登记证据关联，不证明技术结论真实。保留普通文件编辑能力意味着无法阻止人或其他工具绕过管理入口。

历史不是外部防篡改日志，原始操作保存在同一 store 的 history；不要宣称可以检测所有直接离线篡改。正式历史 replay 操作不支持：仅 simulation=true 开放 history，防止模拟导入成为生产放行捷径。

## 分册索引

按当前动作只读对应分册，不通读全部。

| 分册 | 内容 | 主要读者 |
|---|---|---|
| [计划与设计关联](protocol-plan.md) | 计划结构、批次指令与设计书对应、合并审阅、用户指定模型 | 技术负责人；编排者登记模型时 |
| [操作清单与派发登记](protocol-dispatch.md) | 请求与操作表、快捷命令、status/pre_yield 输出 | 编排者；局部任务的技术负责人 |
| [阻塞、裁决与恢复](protocol-decisions.md) | 严重阻塞与临时 workaround、用户离开状态、恢复条件 | 技术负责人；编排者登记信号时 |
| [接管与预算](protocol-budget.md) | 执行者接管、技术预算记账与续作 | 技术负责人规划/诊断时；编排者换人时 |
| [初始化示例](init-example.md) | 已跑通的分批 init 参数 | 技术负责人 |

上下文来源字段见[上下文规则](contexts.md)，追踪字段见[追踪](tracking.md)，证据规则见[工程与验证范围](engineering.md)。
