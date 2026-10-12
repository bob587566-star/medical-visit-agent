# 知禾 · 健康沟通与就诊准备 Agent

一个中文就诊准备与健康资料 Agent。围绕同一份记录，连接多轮对话、原文候选确认、观察时间线、健康资料检索与目标任务执行，支持桌面与手机浏览器。

本项目用于学习、演示与工程验证，尚未经临床专家审校。不能诊断、开处方或决定药物剂量，有限的警示规则不能排除疾病或覆盖全部急症。验收均使用虚构病例。

## 六个功能区域

| 区域 | 可以做什么 |
| --- | --- |
| 总览 | 查看记录、填写进度、观察数量、收藏；继续最近的准备，修改标题 |
| 信息准备 | 保存主要不适、开始时间、年龄阶段、本人感受程度、用药、过敏和既往情况；备注可选 |
| 问答 | 回答已有资料支持的问题，每次只追问一项缺失信息；提取原文候选，确认后才写入表单；来源与工具步骤可展开查看 |
| 时间线 | 保存症状、体温、血压、血糖或其他本人观察，记录时间、单位与备注，可删除；不解释数值 |
| 健康资料库 | 浏览 46 篇、6 类带官方来源的中文教育资料，搜索、分类、查看原文、收藏；本机快照与回滚 |
| 摘要与计划 | 整理已确认自述和观察，生成材料准备步骤与医生提问清单；复制、TXT 下载、打印 |

填写进度按七项基础字段计算，仅表示填写程度。未确认候选不作为已保存事实。修改表单、确认候选或调整时间线后，旧摘要和计划失效，避免继续使用过期内容。

总览和问答内还可启动三种目标任务：整理就诊准备、查找症状相关资料、整理观察时间线。任务先生成受限工具计划，再调用真实执行器；页面显示排队、执行结果和证据对应关系。结果保留为草稿，只有点击确认保存才更新摘要、清单与准备计划，不自动确认聊天候选或修改个人事实。

## 技术与执行方式

前端使用 Vue 3、TypeScript、Vite；后端使用 Python、FastAPI、Pydantic、SQLite、HTTPX 异步客户端与 cryptography。

Agent 调用实际工具：check_urgent_signs、suggest_intake_updates、search_health_knowledge、collect_visit_details、build_visit_checklist、draft_visit_summary、build_preparation_plan。工具输出逐步校验。日常对话由本机流程路由；目标任务可选 DeepSeek 规划工具依赖，最多八步，必须通过工具白名单、唯一性、依赖图和安全步骤前置校验。模型不能提供工具参数、执行代码或写数据库，规划无效或不可用时明确回退为本机计划。

多轮对话把已确认字段和待确认原文候选作为交流上下文，避免反复问已回答的内容；待确认内容仍不进入事实摘要。“没有”和“不知道”只对应当前询问项目，拒绝继续填写会暂停追问。问候、能力询问、资料问题分别处理；只在明确承接上文的问题中使用先前主诉辅助检索，未知地址等问题不会借用旧资料凑答案。发送时先显示本人消息和回复状态，正文优先展示，资料与执行细节按需展开。

检索采用人工主题／别名限制可回答范围，再用 BM25 排序；支持本机 Ollama bge-m3:latest Embedding 与排名融合。固定连接 127.0.0.1:11434，禁止重定向；公共资料向量按内容哈希和模型 digest 缓存，个人问题不写入缓存。未知医院信息、挂号费用和处方请求不凭相似分数拼接答案。排名不作为医学置信度。

Ollama 不可用时自动回退，接口和页面显示真实状态。**当前验收的实际检索模式是 lexical_fallback；混合检索使用模拟接口覆盖，真实 Ollama 调用尚未验收。** 健康教育正文来自整理资料，模型不自由生成诊断和治疗内容。可选 DeepSeek 对话摘录和任务规划均默认关闭，并分别由页面主动选择。

紧急警示先在本机处理，不等待检索或外部模型。规则覆盖胸痛伴危险表现、严重呼吸困难、卒中样表现、严重过敏、意识变化，以及自伤意念、过量服药、抽搐、大量咯血、持续大出血、孕期出血伴危险表现、婴儿发热伴异常反应、头部外伤伴危险表现等有限场景。自伤意念走独立陪伴与求助提示；页面心理支持资源按所选地区显示，不猜测未核实的热线。否定、旧病史、科普与当前报告分开处理，部分情境提示及时联系专业人员而非一律升级急救。来源与适用边界见 [红旗依据](docs/redflag-evidence.md)。

可选本机语义复核默认关闭，仅允许升级求助提示，不能降级规则结果；当前没有完成真实本机模型验收，也未经临床评测，不能把该模块算作已验证的第二道医疗防线。规则未命中、模型可用或有限回归集通过，都不代表安全。

同义表达与引用语境已补最小差异回归：支持“没力气”“不利索”“呼吸也困难”“痰里带血丝”等自述；新闻、书籍、视频、假设与实际当前求助分开处理，实际第三方危险报告不因“朋友”一词而忽略。“活着没意思”等间接表达使用 `prompt + self_harm` 的温和陪伴支持；孤立“我已经准备好了”不判危机，紧邻危机语境另行检查。摘要、准备计划与任务保存统一暂停于需要先获得人类支持的记录，保留聊天、本人原资料及原文导出。依据和分级取舍见 [复审依据](docs/review-safety-boundaries.md)，当前软件验收见 [复审修复记录](artifacts/复审修复验收-20261012.md)。

资料说明与官方来源见 [knowledge/README.md](knowledge/README.md)。reviewed_at 是本项目核对网页的日期，不代表医生审核。

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

安装脚本不会覆盖已有 .env；无需密钥也可使用本机整理、资料查询和导出。需要模型时在本机 .env 填写 DEEPSEEK_API_KEY 和实际可用的 DEEPSEEK_MODEL，重启后在页面主动开启。密钥不返回网页，也不进入 Git 或 Docker 镜像。已运行的本机 Ollama 安装 bge-m3:latest 后，检索器自动检查服务和模型；成功时显示 hybrid_ollama。脚本兼容 Windows PowerShell 5 中文编码，启动关闭访问日志，避免搜索词进入 URL 日志。

Linux/macOS 可创建 .venv，安装 backend/requirements.txt，在 frontend 执行 npm ci 和 npm run build，再运行 .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8270 --no-access-log。

语义复核可选配置为 LOCAL_SAFETY_ENABLED=false 与 LOCAL_SAFETY_MODEL=qwen2.5:7b。它固定连接本机 127.0.0.1:11434，模型不可用时保留本机规则结果。资料版本工具：

```powershell
.\.venv\Scripts\python.exe scripts/knowledge_versions.py snapshot
.\.venv\Scripts\python.exe scripts/knowledge_versions.py list
.\.venv\Scripts\python.exe scripts/knowledge_versions.py rollback '替换为实际版本哈希'
```

回滚只恢复健康资料库，不恢复个人记录。任务保存时校验资料版本；资料更新后需重新运行旧任务。

## 记录与隐私

记录保存在本机 data/visits.sqlite3。访视记录和会话元数据未加密；新增任务结果 payload 使用 Fernet 加密，并绑定访客、任务和记录 ID。任务审计仅记录操作元数据，以本机 HMAC 链和链头检查内容修改及截断；这是本机篡改检测，不能称为外部不可篡改审计。密钥保存在本机被 Git 忽略的数据目录，不是 KMS，也不构成整库加密。

随机 HttpOnly / SameSite Cookie 隔离访客，所有修改接口验证 CSRF 与请求来源；跨访客查询返回 404。访客会话及记录按配置清理，默认 7 天；清理在服务启动和收到请求时执行。浏览器清除 Cookie 后无法再使用该访客记录。

填写页面不收集姓名、电话、证件号或地址，请只用虚构资料演示，避免在自由文本中输入身份信息。主动启用对话外部模型后，仅发送经过限制的词语和时间／程度片段；不发送身份、访客标识、整张表单或完整对话。否定症状与旧病史不会被直接简化成肯定的当前症状。

目标任务的外部规划只发送目标枚举、已填字段名称、观察数量和公开资料 ID，不发送字段值、聊天正文或身份信息。此开关独立于对话模型开关。本机语义复核只发送脱敏并限制长度的当前输入到本机模型，不使用外部医疗语义 API。

删除操作从当前应用数据库删除记录，不承诺清除操作系统残留、导出文件或外部备份。外部模型的政策由服务商决定，本项目未作为真实患者系统部署。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests tests -q --basetemp=.pytest-tmp\manual-run
.\.venv\Scripts\python.exe tests/acceptance.py
.\.venv\Scripts\python.exe tests/acceptance_v2.py
.\.venv\Scripts\python.exe tests/acceptance_conversation.py
.\.venv\Scripts\python.exe tests/acceptance_redflags.py
.\.venv\Scripts\python.exe tests/acceptance_rewording.py
.\.venv\Scripts\python.exe tests/acceptance_tasks.py
.\.venv\Scripts\python.exe tests/model_boundary.py
cd frontend
npm test
npm run build
```

接口测试只对新建的虚构访客操作，结束后清理记录。模型边界测试使用模拟传输，不外发资料；真实服务连通检查需显式运行 tests/live_model.py --external，该脚本只能发送内置虚构输入。

运行中的服务还可以用 scripts/verify.ps1 -Browser 检查后端、模拟模型、HTTP、页面、构建与浏览器流程。浏览器测试使用独立的无登录 Edge，只有虚构资料；其他平台可在 frontend/playwright.config.ts 改用已安装的 Chromium。

历史 V2 结果见 [V2 交付验收](artifacts/交付验收-V2-20261012.md)、[V2 HTTP 结果](artifacts/acceptance_v2.md)、[原 HTTP 回归](artifacts/acceptance_report.md) 与 [模型边界](artifacts/model_boundary_report.md)。独立验收提出的问题保留在 [独立测试发现](artifacts/独立测试发现-20261012.md)。这些文件各自代表运行当时的版本，不作为后续改动已通过的证明。数据流见 [结构说明](docs/architecture.md)，原接口见 [V2 约定](docs/v2-contract.md)，目标任务和警示补充见 [任务与安全流程](docs/tasks-and-safety.md)。

红旗回归包含独立自然语言措辞与聊天、结构化表单入口，检查肯定、否定、历史、科普及及时联系专业人员的边界。通过的是固定测试集合，不能据此声称全部急症召回、临床准确率、医疗认证或不存在所有缺陷。

## 可选 Docker

```powershell
docker compose up -d --build
```

容器入口为 **http://127.0.0.1:8271/**，独立数据卷 visit-data，不会占用本机 8270 或其他客服项目。容器以普通用户运行、根文件系统只读、移除 Linux capabilities；健康检查使用 /readyz。当前交付以本机运行验收为准，Docker 实际构建运行状态见验收记录。

当前是单机、单应用进程的演示实现；没有将本项目实际部署到 Kubernetes，也没有完成临床审核或医疗合规认证。云端使用需要按实际服务范围补身份登录、HTTPS、数据保护、备份恢复和专业审核，不应直接装入真实患者数据。
