# Public OCR dispatch workflow

组织内指定公开仓库的 OCR 调度入口。来源文件：`.github/workflows/codex-code-review.yml`，分支：`main`。

## 管理员接入步骤

1. 在组织 Settings → Rules → Rulesets 新建 Branch ruleset，例如 `OCR public repositories`。
2. Target repositories 使用 Only selected repositories，先选择 `lexmount/lexbench-webfetch`。Target branches 选择 `main`。
3. 勾选 Require workflows to pass before merging，选择来源 `lexmount/github-policy-public`、分支 `main`、文件 `.github/workflows/codex-code-review.yml`。
4. 在组织 Settings → Secrets and variables → Actions 中编辑现有 `OCR_CI_TOKEN`：使用 Selected repositories，并保留已有授权、追加 `lexbench-webfetch`。不要将 Token 写入本仓库。仅 Private repositories 的 Secret 范围不能覆盖公开仓库。
5. 将该公开仓库从原先引用私有 `github-policy` 工作流的 OCR Ruleset 中排除。不要修改其他保护规则。
6. 配置完成后将新规则设为 Active，用新 PR 或新 commit 验证。

本工作流确认后台持久化接单后退出，总提交/轮询时间不超过 90 秒（Runner 排队与启动不计入该计时；Job 硬超时为 3 分钟）。Agent 与默认 OCR 路由由 n8n 决定。最终 `ocr-review` 状态独立回写；当前按建议模式使用，不将它设为 Required。如需强制审查通过再合并，单独添加 required status check。

## 安全与限制

使用 `pull_request_target`，只读取 GitHub 事件中的 PR 坐标，不检出、不导入、不执行 PR 代码，适用于外部 fork PR。权限为 `permissions: {}`；调度令牌仅从目标仓库有权读取的组织 Secret 注入。运行日志不打印令牌。工作流内嵌 Python 标准库客户端，无第三方 Action 或运行时下载依赖。暂不支持 merge queue / merge_group。

公开的内容只有调度客户端、生成器和测试；不包含 OCR 服务代码或凭证。

## 维护与验证

```sh
python3 ocr/render_workflow.py
python3 -m unittest discover -s ocr -p 'test_*.py'
```

编辑 `ocr/ci_client.py` 后重新生成 YAML；测试会检查内嵌客户端一致性。

## 快速回滚

管理员将新 OCR public repositories Ruleset 设为 Disabled，即可解除这条必需工作流约束。保留其他规则和 Secret 原有授权；不要重新让公开仓库引用私有 policy 工作流。该操作不会停止已接收的后台 OCR 任务。
