# 知禾 · 健康沟通工作台 V2

一个中文就诊准备与健康资料 Agent。围绕同一份就诊记录，把聊天整理、信息确认、观察时间线、资料查阅和就诊摘要连接起来，支持桌面与手机浏览器。

本项目用于学习、演示与工程验证，尚未经临床专家审校。不能诊断、开处方或决定药物剂量，有限的警示规则不能排除疾病或覆盖全部急症。验收均使用虚构病例。

## 六个功能区域

| 区域 | 可以做什么 |
| --- | --- |
| 总览 | 查看自己的记录、填写进度、观察数量、收藏；继续最近的准备，修改记录标题 |
| 信息准备 | 保存主要不适、开始时间、年龄阶段、本人感受程度、用药、过敏和既往情况；备注可选 |
| 问答 | 按当前缺失信息逐项追问；提取原文候选，确认后才写入表单；展示来源和实际工具步骤 |
| 时间线 | 保存症状、体温、血压、血糖或其他本人观察，记录时间、单位与备注，可删除；不解释数值 |
| 健康资料库 | 浏览 37 篇、6 类带官方来源的中文教育资料，搜索、分类、查看原文、收藏与取消收藏 |
| 摘要与计划 | 整理已确认自述和观察，生成材料准备步骤与向医生提问的清单；复制、TXT 下载、打印 |

填写进度按七项基础字段计算，仅表示填写程度。未确认的聊天候选不作为已保存事实；空项、未知情况和未核实自述有标注。修改表单、确认候选或调整时间线后，旧摘要和计划失效，避免继续使用过期内容。

## 技术与执行方式

前端使用 Vue 3、TypeScript、Vite；后端使用 Python、FastAPI、Pydantic、SQLite 和 HTTPX 异步客户端。

Agent 使用受限执行器调用实际工具：`check_urgent_signs`、`suggest_intake_updates`、`search_health_knowledge`、`collect_visit_details`、`build_visit_checklist`、`draft_visit_summary`、`build_preparation_plan`。工具输出逐步校验，页面展示实际执行状态。紧急情况与诊断用药请求有独立分支；模型不能自行选择工具或执行代码。

多轮采集依据当前已确认字段追问；已填写的旧问题不再接收下一项短答。“没有”和“不知道”只对应当前询问项目。候选保留原文依据，由使用者确认写入。准备计划依据已确认资料生成，不是诊疗方案。

检索采用人工主题／别名限制可回答范围，再用 BM25 排序；支持本机 Ollama `bge-m3:latest` Embedding 与排名融合。固定连接 `127.0.0.1:11434`，禁止重定向；公共资料向量按内容哈希和模型 digest 缓存，个人问题不写入缓存。未知医院信息、挂号费用和处方请求不凭相似分数拼接答案。检索排名不作为医学置信度。

Ollama 不可用时自动回退，接口和页面显示真实状态。**此次本机运行的是 `lexical_fallback`；混合检索通过模拟接口验证，真实 Ollama 调用尚未验收。** 健康教育正文来自整理资料，模型不自由生成诊断和治疗内容。可选 DeepSeek 只提取有限原文片段，默认关闭；紧急警示先在本机处理，不等待模型。

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

安装脚本不会覆盖已有 `.env`；无需密钥也可使用本机整理、资料查询和导出。需要模型时在本机 `.env` 填写 `DEEPSEEK_API_KEY` 和实际可用的 `DEEPSEEK_MODEL`，重启后在页面主动开启。密钥不返回网页，也不进入 Git 或 Docker 镜像。已运行的本机 Ollama 安装 `bge-m3:latest` 后，检索器自动检查服务和模型；成功时显示 `hybrid_ollama`。脚本兼容 Windows PowerShell 5 中文编码，启动关闭访问日志，避免搜索词进入 URL 日志。

Linux/macOS 可自行创建 `.venv`，安装 `backend/requirements.txt`，在 `frontend` 执行 `npm ci` 和 `npm run build`，再运行 `.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8270 --no-access-log`。

## 记录与隐私

记录保存在本机 `data/visits.sqlite3`，数据库未加密。随机 HttpOnly / SameSite Cookie 隔离访客，所有修改接口验证 CSRF 与请求来源；跨访客查询返回 404。访客会话及记录按配置清理，默认 7 天；清理在服务启动和收到请求时执行。浏览器清除 Cookie 后无法再使用该访客记录。

填写页面不收集姓名、电话、证件号或地址，请只用虚构资料演示，避免在自由文本中输入身份信息。主动启用外部模型后，仅发送经过限制的词语和时间/程度片段；不发送身份、访客标识、整张表单或完整对话。否定症状与旧病史不会被直接简化成肯定的当前症状。

删除操作从当前应用数据库删除记录，不承诺清除操作系统残留、导出文件或外部备份。外部模型的政策由服务商决定，本项目未作为真实患者系统部署。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp=.pytest-tmp\manual-run
.\.venv\Scripts\python.exe tests/acceptance.py
.\.venv\Scripts\python.exe tests/acceptance_v2.py
.\.venv\Scripts\python.exe tests/model_boundary.py
cd frontend
npm test
npm run build
```

接口测试只对新建的虚构访客操作，结束后清理记录。模型边界测试使用模拟传输，不外发资料；真实服务连通检查需显式运行 `tests/live_model.py --external`，该脚本只能发送内置虚构输入。

运行中的服务还可以用 `scripts/verify.ps1 -Browser` 一次检查后端、模拟模型、HTTP、页面、构建与真实浏览器流程。浏览器测试使用独立的无登录 Edge，只有虚构资料；其他平台可在 `frontend/playwright.config.ts` 改用已安装的 Chromium。

最新结果见 [V2 交付验收](artifacts/交付验收-V2-20261012.md)、[V2 HTTP 结果](artifacts/acceptance_v2.md)、[原 HTTP 回归](artifacts/acceptance_report.md) 与 [模型边界](artifacts/model_boundary_report.md)。数据流见 [结构说明](docs/architecture.md)，新接口见 [V2 约定](docs/v2-contract.md)。通过工程测试不代表临床有效性、医疗认证或不存在所有缺陷。

## 可选 Docker

```powershell
docker compose up -d --build
```

容器入口为 **http://127.0.0.1:8271/**，独立数据卷 `visit-data`，不会占用本机 8270 或其他客服项目。容器以普通用户运行、根文件系统只读、移除 Linux capabilities；健康检查使用 `/readyz`。当前交付以本机运行验收为准，Docker 实际构建运行状态见验收记录。

当前是单机、单应用进程的演示实现；云端使用需要另外补身份登录、HTTPS、数据保护与访问审计、备份恢复和医疗专业审核，不应直接装入真实患者数据。
