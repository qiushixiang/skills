# Skills

可复用 Agent 技能与配套脚本。

## 当前技能

- [batch-flow-orchestration](batch-flow-orchestration/SKILL.md)：分批任务编排、技术负责人裁决、独立审阅、预算配套、Linux CLI 派发及 AGY 主会话门禁。

## 开发与验证

从仓库根目录运行离线测试：

```sh
python3 -m unittest discover -s batch-flow-orchestration/scripts -p 'test_*.py'
```

真实 CLI 验收会调用模型，按已获授权的模型配置显式执行：

```sh
python3 batch-flow-orchestration/scripts/test_agent_cli_live.py \
  --backend <agy|codex|claude> --model <已批准模型> --effort <已批准值或default>
```

修改入口在 `batch-flow-orchestration/`，同时更新相关指南与回归测试。验证通过后提交明确的变更说明，保持提交可追溯。项目启动入口可使用指向技能脚本的相对软链接，无需复制脚本。

任务台账、原始代理会话、CLI profiles、认证资料和一次性任务修复脚本不属于技能源码，不提交到本仓库。
