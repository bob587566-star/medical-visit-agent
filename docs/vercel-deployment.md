# Vercel 完整应用部署

本仓库将 Vue 页面与 FastAPI API 部署在同一个 Vercel 项目、同一个 HTTPS 域名下。配置文件并不代表线上服务已经发布或通过验收；访问地址只在实际部署成功并核对后公布。请使用虚构资料验证。

## 必备资源

部署需要可访问的 Vercel 账号，以及独立的云端 PostgreSQL 数据库、支持标准 Redis TCP 协议的 TLS Redis 服务。Redis 需要支持 Streams、消费者组、Lua 与带过期时间的锁；只有 REST 接口或不支持这些命令的缓存不能替代它。

云端数据库应是本项目专用的新数据库。部署不会上传或导入当前本机的 SQLite 记录、账号、聊天、密钥或备份。本机账号也不会自动在云端出现；云端首次访问从新的访客身份开始。

在 Vercel 项目的 Environment Variables 中配置以下值，并按需要分别配置 Production 与 Preview 环境：

| 变量 | 要求 |
| --- | --- |
| `DATABASE_URL` | 远程 PostgreSQL 连接串，支持 `postgres://`、`postgresql://` 或 `postgresql+psycopg://`，内部统一使用 psycopg；必须设置 `sslmode=require`、`verify-ca` 或 `verify-full`，建议使用连接池入口 |
| `REDIS_URL` | 远程 Redis 的 `rediss://` TLS 连接串，含正确的认证信息 |
| `MEDICAL_DATA_KEY` | 稳定的 Fernet 数据加密密钥 |
| `MEDICAL_TASK_KEY` | 单独保存的稳定 Fernet 任务加密密钥 |
| `PUBLIC_APP_ORIGIN` | 实际公开访问地址的精确 HTTPS origin，仅含协议与域名；不含路径、查询或通配符 |
| `APP_ALLOWED_HOSTS` | 可选，逗号分隔的精确域名；自定义域名须在这里显式加入，不接受 `*` 或 `*.vercel.app` |
| `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` | 可选；不配置时规则整理与资料检索仍可使用，外部模型功能关闭 |

Vercel 自动提供 `VERCEL=1`、`VERCEL_URL` 等平台变量。应用只接受平台当前部署域名、项目生产域名与显式配置的域名。`PUBLIC_APP_ORIGIN` 必须对应允许的实际域名；预览页面也须使用与其访问入口匹配的 origin 和独立测试资源。

Fernet 密钥使用 32 字节随机材料的 URL-safe Base64 编码，建议分别生成两把密钥并存入受控的密码管理器与 Vercel 环境变量。它们必须在后续部署及多个实例间保持一致；丢失密钥会使既有密文不可读，重新生成密钥不能恢复记录。不要上传本机密钥文件，也不要设置 `MEDICAL_DATA_KEY_FILE`、`MEDICAL_TASK_KEY_FILE` 或 `VISIT_DATA_PATH`。缺少必需配置会拒绝启动，不回退到临时 SQLite。

## 构建与发布

项目 Root Directory 选仓库根目录，Framework Preset 使用 **FastAPI**。`pyproject.toml` 将 Python 固定在 3.12，列出运行依赖并通过 `[tool.vercel] entrypoint = "backend.main:app"` 指向实际后端。`vercel.json` 的构建命令在 `frontend` 中执行 `npm ci` 与 `npm run build`，生成 `frontend/dist`。构建环境需要 Node.js 22 或更高版本。

保留仓库中的 FastAPI 配置；不将 Output Directory 改成纯前端静态发布。Vue 页面由同一 FastAPI 服务提供，API、会话 Cookie、CSRF 与来源检查共用同一域名。函数保留健康教育资料与构建后的前端文件，并排除测试、验收产物、本机环境文件、数据库、日志、备份和缓存。`.vercelignore` 同时限制 CLI 上传内容。

已登录 Vercel CLI 并完成项目关联、云端资源及环境变量配置后，可在仓库根目录执行：

```powershell
npx vercel deploy --prod
```

也可从 Vercel 导入仓库并使用同样的项目设置。环境变量更新后需要重新部署。不要把环境密钥写入 Git、命令行参数、提交记录或公开验收日志。

根据 [Vercel FastAPI 官方说明](https://vercel.com/docs/frameworks/backend/fastapi)，自定义入口可放在 `pyproject.toml`；静态文件直接进入 CDN 会绕过应用中间件。本项目显式设置 `cdn = false`，使页面与静态资源继续经过现有 Host 检查和安全响应头。函数设置 `maxDuration = 300`；实际限额以项目套餐与 Fluid compute 设置为准，见 [函数时限](https://vercel.com/docs/functions/configuring-functions/duration)。

## 云端运行行为

健康自述、账号与任务记录保存在 PostgreSQL，任务投递及共享限流使用 Redis。函数的 `/tmp` 只用于可丢弃的运行文件，不能作为持久记录或密钥存储。云端不连接本机 Ollama，也不启动本机语义复核；健康资料使用主题与词法检索。页面的保存同意与处理位置根据后端返回的 `storage_mode` 和 `storage_label` 显示。

Vercel 中的目标任务在已认证请求内等待执行：单次引擎执行限制 85 秒，请求推进限制 90 秒。任务记录、投递标识与租约保存在外部服务；请求中断后，本人重新打开记录或轮询该任务时会检查并继续处理，未过期的租约会避免重复执行。此方式不提供无人访问时持续运行的独立 worker；关闭页面后不能保证任务会自行完成。需要持续处理队列时，使用已有 PostgreSQL／Redis 部署并单独运行 `backend.worker`，见 [安全与部署](security-and-operations.md)。

## 部署后核对

使用实际公布的 HTTPS 地址，在新的浏览器会话中核对：

1. `/readyz` 返回就绪，页面与构建资源正常打开；HTTP 检查不输出密钥、Cookie 或患者内容。
2. 隐私同意项显示云端服务与云端数据库；未同意时不自动建立健康记录，公开资料仍可浏览。
3. 使用虚构资料确认保存、查询、摘要、任务与手动确认任务草稿；刷新后同一身份仍能读取自己资料。
4. 验证昵称注册、登录、退出、下载、撤回同意，以及另一独立访客无法读取本人记录。
5. 检查 Cookie 的 Secure／HttpOnly／SameSite、安全响应头、CSRF 与精确来源校验；检查不含本机资料的部署包。

本机测试、模拟云端配置和构建通过，只证明对应检查范围。真实 PostgreSQL／Redis 连通、Vercel 构建、线上任务恢复与手机浏览器表现，需要在实际部署中分别核对。
