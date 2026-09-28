# 贡献指南

感谢你对 OpenKefu 的关注！欢迎通过以下方式参与贡献。

## 提交 Issue

- **Bug 反馈**：请使用 [bug 模板](.github/ISSUE_TEMPLATE/bug_report.md)，附上复现步骤、期望行为与实际行为、日志片段（注意脱敏）。
- **功能建议**：请使用 [feature 模板](.github/ISSUE_TEMPLATE/feature_request.md)，描述使用场景与期望方案。

提交前请先搜索已有 issue，避免重复。

## 开发流程

1. Fork 仓库并创建特性分支：

```bash
git checkout -b feat/your-feature
```

2. 完成开发并保证质量：

```bash
# 后端：编译检查 + 离线单元测试
python -m compileall openkefu run_web.py run_runtime_worker.py
PYTHONPATH=. python checks/security_unit.py
# ...（checks/ 下全部 *_unit.py，有 MySQL 环境时再跑 checks/e2e_local_test.py）

# 前端
cd web && npm run build
```

3. 提交 PR，描述清楚改动内容与验证方式。

## 代码约定

- Python 后端遵循现有代码风格（类型标注、中文注释用于业务说明）。
- API 路由按域拆分在 `openkefu/web/routers/`，跨域共享逻辑放 `openkefu/web/context.py`。
- 新平台接入放 `openkefu/platforms/<平台名>/`，不要把平台逻辑混入 `openkefu/web/`。
- 提交信息使用 conventional commits 风格（`feat:` / `fix:` / `docs:` / `refactor:`）。

## 安全须知

- **绝不提交** `config.local.json`、API Key、密码、真实店铺数据。
- 发现安全漏洞请勿公开 issue，私信维护者。
