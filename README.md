<div align="center">

# OpenKefu

**多平台电商店铺 AI 客服管理台**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)

首个支持平台：**拼多多** · 更多平台开发中

</div>

---

## 项目简介

OpenKefu 是一个开箱即用的电商店铺客服自动化管理台：在自建 Web 控制台上管理多个店铺的客服连接，接入大模型自动回复，支持人工接管、客服转接、知识库检索与退换货记录管理。

```
Web 控制台（React） ── FastAPI 后端 ── 店铺运行时（ShopRunner）
                                        │
                                        ├─ platforms/pdd ── 拼多多客服（扫码/账密登录、Titan WebSocket）
                                        └─ platforms/... ── 预留更多电商平台
```

核心能力：

- **多店铺管理**：创建店铺、扫码/账密登入、上线/下线客服连接、登录态过期自动续登。
- **实时客服聊天**：全部店铺消息聚合展示，可按店铺筛选；文本、图片、商品卡片、链接、系统消息全类型适配，保留原始 JSON。
- **AI 自动回复**：按店铺开关；意图识别 → 知识库检索 → 大模型生成，附带会话最近上下文；命中快捷回复直接发送。
- **人工接管**：一键转人工 / 转接指定客服，人工回复与 AI 回复自动协调。
- **知识库**：文档上传、问答对维护、向量检索（MySQL 持久化）。
- **退换货记录**：从聊天中提取退换/改地址诉求，结构化记录、检索与 Excel 导出。
- **多用户与配额**：管理员/客服角色，店铺/知识库数量与 LLM 调用配额管理。
- **可观测性**：运行日志入库与检索、服务器状态监控、实时 WS 推送。
- **生产就绪**：api/worker 分离部署、Redis 命令总线、店铺租约高可用、systemd/nginx 部署模板。

## 快速开始（本机体验）

### 环境要求

- Python 3.11+，Node.js 18+
- MySQL 8.x（本机 `127.0.0.1:3306`）
- Redis（api/worker 分离部署时必需；单机 both 模式可选）
- 网络可访问拼多多商家后台与你配置的大模型 API

### 1. 安装依赖

```bash
pip install -r requirements.txt

cd web
npm install
npm run build
cd ..
```

### 2. 创建配置

```bash
cp config.example.json config.local.json
# 编辑 config.local.json：填写 MySQL 密码、llm.api_key，
# 并生成三个随机密钥：jwt_secret / runtime.command_secret / security.data_encryption_key
```

三个密钥必填（缺失会拒绝启动），每台部署必须使用不同的随机值。也可用环境变量 `OPENKEFU_JWT_SECRET` / `OPENKEFU_RUNTIME_COMMAND_SECRET` / `OPENKEFU_DATA_ENCRYPTION_KEY` 覆盖。

### 3. 启动

```bash
python run_web.py --prod --role both
```

浏览器打开 `http://127.0.0.1:8000`。首次打开会显示"首次设置管理员"表单（仅允许从本机回环地址发起），创建后进入管理台。

### 4. 接入店铺

1. 打开"店铺管理"→ 新建店铺
2. 点击"登入"，用手机拼多多 App 扫码（或使用账密登录）
3. 登录成功后自动上线客服连接
4. 进入"客服聊天"，开启自动回复或人工接待

## 部署

### Linux（Ubuntu/Debian，api + worker 分离，推荐）

```bash
# 系统依赖
sudo apt update && sudo apt install -y git python3-venv python3-pip nodejs npm mysql-server redis-server

# 拉取代码并安装
git clone <你的仓库地址> openkefu && cd openkefu
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd web && npm ci --no-audit --no-fund && npm run build && cd ..

# 配置
cp config.example.json config.local.json && vi config.local.json

# 启动（方式一：单进程 both 模式）
OPENKEFU_RUNTIME_ROLE=both .venv/bin/python run_web.py --prod --role both

# 方式二（推荐）：systemd 服务，模板见 deploy/systemd/
```

部署注意事项：

- `deploy/systemd/` 提供 api/worker 的 systemd 服务模板；`deploy/test-ops/` 提供测试环境一键部署/回滚脚本。
- `deploy/nginx/openkefu.conf.example` 提供反向代理示例（`/api/` 与 `/ws` 都要转发）。
- api 与 worker 分离部署时，`redis.url` 必须带密码（ACL），且 `runtime.worker_id` 每台机器保持唯一。
- 服务重启后，店铺按租约自动恢复在线（登录态失效的店铺会安全下线，需重新登入）。

### Windows 长期运行

```powershell
python run_web.py --prod --role both
```

可用任务计划程序设置开机自启（触发器：用户登录时；起始于：项目根目录）。公网部署不建议直接暴露服务，请配置 HTTPS 反向代理与访问控制。

## 配置说明

全部配置集中在 `config.local.json`（不入库），模板见 [config.example.json](config.example.json)。主要字段：

| 字段 | 说明 |
|---|---|
| `mysql` | 数据库连接（启动时自动建库建表） |
| `redis.url` | Redis 连接（api/worker 分离时必填带 ACL 密码） |
| `runtime.role` | `both` / `api` / `worker` 部署角色 |
| `runtime.command_secret` | api ↔ worker 命令总线密钥 |
| `security.jwt_secret` | 登录 JWT 签名密钥 |
| `security.data_encryption_key` | 店铺登录缓存加密密钥（更换后已存登录态失效） |
| `llm` | 大模型 API（默认 DeepSeek，可换任何 OpenAI 兼容接口） |
| `embedding` | 向量模型 API（知识库检索用） |

环境变量前缀统一为 `OPENKEFU_`（如 `OPENKEFU_RATE_LIMIT_DISABLED=1` 可在测试环境关闭登录限流）。

## 项目结构

```
.
├── run_web.py                  # Web 后端入口（--role both/api/worker）
├── run_runtime_worker.py       # 独立 runtime worker 入口
├── openkefu/
│   ├── platforms/              # 平台适配层
│   │   └── pdd/                # 拼多多：auth（登录/风控）+ chat（客服/Titan WS）
│   ├── web/
│   │   ├── app.py              # FastAPI 装配（中间件/WS/静态资源）
│   │   ├── context.py          # 共享状态与跨域辅助
│   │   ├── routers/            # API 路由（auth/users/shops/chat/...）
│   │   ├── runtime/            # 店铺运行时（ShopRunner + Manager）
│   │   ├── db.py / realtime.py / runtime_bus.py / security.py / ...
│   └── config.py               # 包级路径常量（见 platforms/pdd/config.py）
├── web/                        # React + Vite + TypeScript 前端
├── deploy/                     # systemd / nginx / redis / 测试运维脚本
├── checks/                     # 单元测试与 e2e 脚本
└── docs/                       # 文档
```

## 如何接入新平台

项目预留了平台适配层。接入新平台的基本步骤：

1. 在 `openkefu/platforms/` 下新建平台包（参考 `pdd/`），实现：
   - **登录**：获取并维护平台登录态（cookie/token），支持加密缓存
   - **消息通道**：平台 IM 协议的收发（参考 `pdd/chat/titan_ws_client.py`）
   - **业务接口**：发消息、会话列表、商品、订单、转接等（参考 `pdd/chat/customer_service.py`）
2. 复用 `openkefu/web/` 通用层：多用户、配额、知识库、实时推送、租约高可用均与平台无关。
3. 在 ShopRunner 中接入新平台的启动/重连/下线流程。

## 开发

```bash
# 后端 + 前端开发模式（Vite 热更新）
python run_web.py                    # 终端 1：后端 :8001
cd web && npm run dev                # 终端 2：前端 :5173（代理 /api 与 /ws）

# 运行离线单元测试
PYTHONPATH=. python checks/security_unit.py
PYTHONPATH=. python checks/send_recovery_unit.py
# ...（checks/ 下全部 *_unit.py）

# 本地 e2e（需要 MySQL）
PYTHONPATH=. python checks/e2e_local_test.py
```

## 店铺状态说明

| 状态 | 含义 |
|---|---|
| 未启动 | 店铺刚创建，还没有登入 |
| 登入中 / 待扫码 | 正在生成或等待扫码 |
| 在线 | 客服连接已建立，可收发消息 |
| 已下线 | 主动断开，登录缓存仍有效 |
| 异常 | 连接失败或登录失效，需重新登入 |

## ⚠️ 免责声明

本项目仅供**学习与研究**用途。使用者需自行承担使用本项目的所有风险与责任，包括但不限于：

- 自动登录、消息收发等功能的实现涉及对平台接口的逆向分析，可能违反相关平台的用户协议；
- 使用自动化工具处理客服业务可能违反平台规则，导致账号受限或封禁；
- 请遵守所在地区法律法规，规范使用。

本项目与拼多多等任何电商平台无关联。若平台方提出异议，本项目将配合处理。

## 许可证

[MIT](LICENSE) © 2026 Chen Wenjun
