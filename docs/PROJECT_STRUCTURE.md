# OpenKefu 项目结构分类说明

> 按 **类型 → 模块 → 功能** 三层分类。`[入库]` = 随 git 提交；`[不入库]` = 已 gitignore。

## 一、入口脚本（仓库根目录，`[入库]`）

| 文件 | 类型 | 功能 |
|---|---|---|
| `run_web.py` | 服务入口 | Web 管理台后端（`--role both/api/worker`、`--mode dev/test/prod`，兼容 `--test/--prod`） |
| `run_runtime_worker.py` | 服务入口 | 独立 runtime worker（接收 Redis 命令、跑店铺运行态） |
| `main.py` | 旧版入口 | 早期命令行登录/调试入口（保留兼容） |

## 二、配置

| 文件 | 类型 | 功能 |
|---|---|---|
| `config.local.json` | 唯一配置 `[不入库]` | 全部运行配置：server/mysql/redis/runtime/security/logging/llm/embedding/vector_store/storage。含 MySQL 口令、API Key 与三个必填随机密钥（jwt_secret / command_secret / data_encryption_key） |
| `config.example.json` | 配置模板 `[入库]` | 首次部署复制为 `config.local.json` 后填写 |

## 三、核心包 `openkefu/`

### 3.1 `platforms/` — 平台适配层（`[入库]`）

| 模块 | 功能 |
|---|---|
| `platforms/pdd/auth/` | 拼多多扫码/账密登录、anti-content 风控参数（Node 桥执行 login_js）、`mms_b84d1838` cookie 算法复现、pfb 行为上报编解码 |
| `platforms/pdd/chat/` | 客服业务：Titan WS 客户端与编解码、客服 HTTP 接口（发消息/会话/商品/转接）、自动回复链路（意图→知识库→LLM→缓存）、订单/退货/快捷回复封装、字体反爬解码 |
| `platforms/pdd/config.py` | 拼多多平台常量（URL、UA、login_js 路径、PROJECT_ROOT） |
| `platforms/pdd/common.py` | 拼多多公共工具（cookie 别名、请求封装） |

> 接入新平台：在 `platforms/` 下新建包，实现登录、消息通道与业务接口，复用 `openkefu/web/` 通用层。

### 3.2 `web/` — 通用后端服务层（平台无关，`[入库]`）

| 模块 | 文件 | 功能 |
|---|---|---|
| 装配 | `app.py` | FastAPI 应用工厂：中间件、CORS、WS 端点、静态资源、lifespan |
| 共享上下文 | `context.py` | AppContext：共享状态（db/hub/runtime/服务）+ 跨域辅助（认证依赖、权限、会话查询、退货记录） |
| 路由 | `routers/` | 按域拆分的 APIRouter：auth / users / shops / chat / return_records / knowledge / logs / status（每个模块暴露 `build_router(ctx)`） |
| 数据访问 | `repositories/` | 按域收敛全部 SQL：UsersRepository（users/app_settings/shop_assignments）、ShopsRepository（shops/sessions/login_caches/leases/qr/notes）、ConversationsRepository（conversations/messages/尝试记录）、ReturnRecordsRepository、RuntimeLogsRepository |
| 无状态工具 | `deps.py` | client_ip / is_loopback_ip / validate_user_password |
| 运行时 | `runtime/shop_runner.py` | ShopRunner：单店铺客服连接生命周期、自动回复调度 |
| 运行时 | `runtime/manager.py` | ShopRuntimeManager：多店铺管理、租约/心跳、后台服务 |
| 运行时 | `shop_runtime.py` | 兼容 shim，re-export 上述两个类 |
| 数据库 | `db.py` | MySQL 连接池、自动建库建表、迁移、查询封装 |
| 实时推送 | `realtime.py` | 前端 WebSocket 事件推送（Redis 订阅 + 重连） |
| 命令总线 | `runtime_bus.py` | API ↔ worker 的 Redis 命令通道（含断线重连） |
| 安全 | `security.py` / `crypto.py` | JWT 签发校验、口令哈希、cookie 加密（Fernet） |
| 限流 | `ratelimit.py` | 内存固定窗口限流 |
| 配置 | `config.py` | AppConfig 加载/校验（唯一入口 config.local.json，环境变量前缀 `OPENKEFU_`） |
| 日志 | `logging_config.py` | 日志格式/滚动配置 |

## 四、前端 `web/`（`[入库]`）

| 目录/文件 | 功能 |
|---|---|
| `src/App.tsx` | 应用入口与路由/布局 |
| `src/components/` | 页面组件：Auth / Chat / Knowledge / Logs / Shop / Users / ReturnRecords / Profile / QrLogin / PasswordLogin / TransferSettings / StatusBadge / Toast / NavButton |
| `src/types/types.ts` | TypeScript 类型定义 |
| `src/utils/` | 常量、辅助函数、pending 操作 hook |
| `package.json` / `vite.config.ts` 等 | 构建配置（lock 文件入库，Linux 用 `npm ci`） |
| `dist/` | 构建产物 `[不入库]` |

## 五、部署 `deploy/`（`[入库]`）

| 目录 | 功能 |
|---|---|
| `systemd/` | 生产 systemd 服务模板（api / runtime） |
| `nginx/` | 反向代理配置示例（`/api/` 与 `/ws` 转发） |
| `mysql/`、`redis/` | MySQL / Redis 配置示例 |
| `test-ops/` | 测试环境一键部署/回滚（bin 脚本 + systemd + redis 配置） |

## 六、测试 `checks/`（`[入库]`）

| 文件 | 覆盖 |
|---|---|
| `intent_prompt_unit.py` | 意图提示词构造 |
| `realtime_unit.py` | 实时推送模块（含 Redis 重连） |
| `return_records_unit.py` | 退货记录解析 |
| `security_unit.py` | 安全/配置校验 |
| `db_pool_unit.py` | 连接池 |
| `chat_transport_unit.py` | 聊天传输 |
| `reply_cache_unit.py` | 回复缓存 |
| `send_recovery_unit.py` | 发送恢复/重连 |
| `run_web_unit.py` / `frontend_contract_unit.py` / `frontend_chat_unit.cjs` | 入口与前端契约 |
| `e2e_local_test.py` | 本地 API/WS e2e（需 MySQL） |

运行方式：`PYTHONPATH=. python checks/<脚本>`；CI 见 `.github/workflows/ci.yml`。

## 七、文档与社区文件（`[入库]`）

| 文件 | 内容 |
|---|---|
| `README.md` | 项目说明 / 快速开始 / 部署 / 免责声明 |
| `LICENSE` | MIT 许可证 |
| `CONTRIBUTING.md` | 贡献指南 |
| `.github/` | CI 工作流 + Issue 模板 |
| `docs/OpenKefu_客服快速上手指南.docx` | 客服使用指南 |
| `docs/PROJECT_STRUCTURE.md` | 本文档 |

## 八、运行时数据（`[不入库]`，目录自动创建）

| 目录/位置 | 内容 |
|---|---|
| MySQL `knowledge_chunks.embedding_json` | 知识库向量数据 |
| `data/knowledge_files/` | 知识库文档上传目录 |
| `qrcodes/` | 登录二维码 PNG |
| `logs/` | 运行日志 |

## 九、git 上传清单速览

**上传**：入口脚本、`config.example.json`、`openkefu/`、`web/`（除 dist/node_modules）、`deploy/`、`checks/`、`docs/`、`.github/`、`requirements.txt`、`.gitignore`、`.gitattributes`、`README.md`、`LICENSE`、`CONTRIBUTING.md`

**不上传**：`config.local.json`（含密钥）、`logs/`、`data/`、`qrcodes/`、`temp/`、`temp_api/`、`*.log`、`__pycache__/`、`.venv/`、`web/node_modules/`、`web/dist/`、`.npm-cache/`、`.idea/`、`.vscode/`、`.claude/`
