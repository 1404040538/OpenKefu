# OpenKefu 测试环境一键运维框架

这个目录保存测试环境的自动同步、发布、启动和回滚框架。当前 Linux 测试环境实际部署路径：

```bash
/home/deploy/openkefu-test-deploy
```

## 一键部署

测试环境手动更新并重启：

```bash
~/bin/deploy-openkefu-test
```

脚本默认只允许测试环境执行：

```bash
OPENKEFU_DEPLOY_ENV=test
```

部署流程：

1. 从 Gitee 拉取 `main` 最新提交；
2. 将 `/home/deploy/OPENKEFU_agent_test` 同步到 `origin/main`；
3. 创建新的 release 目录；
4. 安装 Python / Node 依赖并构建前端；
5. 执行基础检查；
6. 切换 `current`；
7. 重启 Redis / API / Worker；
8. 执行健康检查；
9. 如果失败，自动回滚到上一个 release，并删除失败 release，避免 `.venv` 和 `node_modules` 持续堆积。

如果 `OPENKEFU_DEPLOY_ENV` 不是 `test`，脚本会拒绝自动同步和重启，避免误用于生产环境。

## 自动轮询部署

自动轮询入口：

```bash
/home/deploy/openkefu-test-deploy/bin/auto-deploy-openkefu-test
```

它的行为是：

1. 使用 `auto-deploy.lock` 避免多个部署任务重叠执行；
2. `git fetch origin main`；
3. 对比 `origin/main` 与 `current/REVISION`；
4. 如果提交一致，直接退出；
5. 如果发现新提交，执行 `OPENKEFU_DEPLOY_ENV=test ~/bin/deploy-openkefu-test`。

查看 systemd timer：

```bash
systemctl --user status openkefu-test-auto-deploy.timer
```

查看自动部署日志：

```bash
tail -f /home/deploy/openkefu-test-deploy/shared/auto-deploy.log
```

## 目录结构

```text
/home/deploy/openkefu-test-deploy
├── bin/
│   ├── deploy-openkefu-test
│   └── auto-deploy-openkefu-test
├── current -> releases/<timestamp>-<commit>
├── releases/
└── shared/
    ├── config.local.json
    ├── redis.conf
    ├── service.env
    └── auto-deploy.log
```

## 关键约束

- 测试环境可以自动拉取 Git、构建并重启；
- 生产环境不走这个自动同步逻辑；
- Redis 密码、运行时配置等敏感内容只保存在服务器 `shared/` 目录，不提交到 Git；
- Cloudflare 临时代理只代理本机测试端口，不改变业务服务本身；
- 发布失败会回滚并删除失败 release，避免磁盘被失败构建产物占满。

## 常用命令

检查服务：

```bash
systemctl --user status openkefu-test-api.service
systemctl --user status openkefu-test-worker.service
systemctl --user status openkefu-test-redis.service
systemctl --user status openkefu-test-cf-tunnel.service
```

只重启测试环境，不拉取代码：

```bash
systemctl --user restart openkefu-test-redis.service
systemctl --user restart openkefu-test-api.service
systemctl --user restart openkefu-test-worker.service
```

检查健康状态：

```bash
curl -fsS http://127.0.0.1:18000/api/auth/status
```

检查磁盘：

```bash
df -h /
du -sh /home/deploy/openkefu-test-deploy/releases
```
