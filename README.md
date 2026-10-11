# 知禾 · 就诊准备 Agent

一款中文就诊准备与健康资料助手。把零散自述整理成给医生看的资料，保留哪些是本人填写、哪些尚未核实；常见健康问题可查看带来源链接的教育资料。

本项目用于学习、演示与工程验证，尚未经临床专家审校。不能诊断、开处方或决定药物剂量，有限的警示规则不能排除疾病或覆盖全部急症。验收均使用虚构病例。

## 已实现功能

- **就诊记录**：填写主要不适、开始时间、年龄阶段、药物、过敏和既往情况；新建、切换、保存及删除记录。
- **健康资料问答**：查询已核对来源的简短中文资料，展示来源、核对日期和链接。没有对应资料时明确说明；问候不会拼接无关医学内容。
- **警示提醒**：先检查当前自述中的有限紧急征象；命中后立即提示联系当地急救，不等待大模型。
- **准备清单与摘要**：依据实际填表和原始自述生成；空项标为未填写，清单可勾选，摘要可复制、下载 TXT 和打印。
- **可选模型辅助**：复用本机配置的 DeepSeek，提取有限的症状、时间和程度片段；只接受输入中真实存在的片段，异常或超时回退本机。默认不调用外部模型。
- **手机与桌面页面**：准备、问答、摘要三个入口，较大正文、明确的加载和错误状态。

## 技术与执行方式

前端使用 Vue 3、TypeScript、Vite；后端使用 Python、FastAPI、Pydantic、SQLite 和 HTTPX 异步客户端。

Agent 使用受限执行器调用实际工具：`check_urgent_signs`、`search_health_knowledge`、`collect_visit_details`、`build_visit_checklist`、`draft_visit_summary`。工具输出逐步校验，页面展示已执行工具的状态。紧急情况与诊断用药请求有独立分支；模型不能自行选择工具或执行代码。

检索采用人工主题/别名筛选候选，再用 BM25 排序。这是小型、人工整理的资料库，当前没有向量模型、Rerank、全文医学库或医院业务系统接口。健康教育正文来自整理过的资料，模型不自由生成诊断和治疗内容；摘要直接依据自述生成。

资料说明与官方来源见 [knowledge/README.md](knowledge/README.md)。`reviewed_at` 是本项目核对网页的日期，不代表医生审核。

## 本机启动

Windows 需要 Python 3.11+ 和 Node.js 22+。在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1
```

打开 **http://127.0.0.1:8270/**。停止服务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/stop.ps1
```

安装脚本不会覆盖已有 `.env`；无需 API Key 也可完整使用本机整理、资料查询和导出。需要模型时在本机 `.env` 填写 `DEEPSEEK_API_KEY` 和实际可用的 `DEEPSEEK_MODEL`，重启后在页面主动开启。密钥不返回到网页，也不进入 Git 或 Docker 镜像。

Linux/macOS 可自行创建 `.venv`，安装 `backend/requirements.txt`，在 `frontend` 执行 `npm ci && npm run build`，再运行 `.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8270`。

## 记录与隐私

记录保存在本机 `data/visits.sqlite3`，数据库未加密。随机 HttpOnly / SameSite Cookie 隔离访客，所有修改接口验证 CSRF 与请求来源；跨访客查询返回 404。访客会话及记录按配置清理，默认 7 天；清理在服务启动和收到请求时执行。浏览器清除 Cookie 后无法再使用该访客记录。

填写页面不收集姓名、电话、证件号或地址，请只用虚构资料演示，避免在自由文本中输入身份信息。主动启用外部模型后，仅发送经过限制的词语和时间/程度片段；不发送身份、访客标识、整张表单或完整对话。否定症状与旧病史不会被直接简化成肯定的当前症状。

删除操作从当前应用数据库删除记录，不承诺清除操作系统残留、导出文件或外部备份。外部模型的政策由服务商决定，本项目未作为真实患者系统部署。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp=.pytest-tmp\manual-run
.\.venv\Scripts\python.exe tests/acceptance.py
.\.venv\Scripts\python.exe tests/model_boundary.py
cd frontend
npm test
npm run build
```

接口测试只对新建的虚构访客操作，结束后清理记录。模型边界测试使用模拟传输，不外发资料；真实服务连通检查需显式运行 `tests/live_model.py --external`，该脚本只能发送内置虚构输入。

运行中的服务还可以用 `scripts/verify.ps1 -Browser` 一次检查后端、模拟模型、HTTP、页面、构建与真实浏览器流程。浏览器测试使用独立的无登录 Edge，只有虚构资料；其他平台可在 `frontend/playwright.config.ts` 改用已安装的 Chromium。

完整结果见 [本机交付验收](artifacts/交付验收-20261012.md)、[HTTP 结果](artifacts/acceptance_report.md) 与 [模型边界](artifacts/model_boundary_report.md)。通过工程测试不代表临床有效性、医疗认证或不存在所有缺陷。

## 可选 Docker

```powershell
docker compose up -d --build
```

容器入口为 **http://127.0.0.1:8271/**，独立数据卷 `visit-data`，不会占用本机 8270 或其他客服项目。容器以普通用户运行、根文件系统只读、移除 Linux capabilities；健康检查使用 `/readyz`。当前交付以本机运行验收为准，Docker 实际构建运行状态见验收记录。

当前是单机、单应用进程的演示实现；云端使用需要另外补身份登录、HTTPS、数据保护与访问审计、备份恢复和医疗专业审核，不应直接装入真实患者数据。
