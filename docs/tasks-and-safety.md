# 目标任务、异步接口与警示流程

本文补充原 [接口约定](contract.md) 与 [V2 接口](v2-contract.md)。项目用于虚构资料的工程验证，结果是就诊准备和公共健康资料整理，不是诊断、处方或诊疗方案。

## 对话与本人确认

消息的 followup_field 标明当前追问字段，避免把“没有”“不知道”等短答错写到其他项目。未确认候选可以帮助对话跳过重复问题，但仍不进入已保存事实、摘要或准备计划。候选的 evidence 与 value 保留实际原文，确认接口使用后端保存的候选；暂停采集后不继续追问。资料、候选与工具步骤默认折叠，先展示答复正文。

Risk 新增兼容字段 kind: medical | self_harm | unknown。level 仍为 emergency | prompt | unknown，未知表示未确定，不表示安全。自伤危机使用 self_harm 和独立支持文本；普通紧急表现使用 medical。旧记录缺少 kind 时按 schema 默认值读取。

## 任务目标与计划

| goal | 输出范围 |
| --- | --- |
| prepare_visit | 已保存自述与观察的摘要、待补项目、准备清单及医生提问 |
| understand_symptom | 对应已核对资料与可追溯摘录；无证据时明确说明 |
| organize_timeline | 按实际记录时间整理观察与记录缺口，不解释测量数值 |

任务不能自动确认候选、修改健康资料库、执行程序、发送消息或给医学结论。默认本机计划；请求 use_model: true 才尝试外部规划，该授权独立于对话开关。外部请求只包含目标枚举、已填字段名称、观察数量和公开资料 ID，不发送字段值、完整对话、个人身份或访客标识。

规划返回工具和依赖关系，经 Pydantic 和依赖图校验后由实际 Executor 执行。最多八步，禁止重复工具、未知工具、循环依赖和额外参数。安全检查必须先执行；后续步骤必须依赖安全检查，摘要和计划必须依赖资料读取。模型无效、不可用或超时则显示 fallback，不会假装采用模型计划。

每步保存实际状态、输出、来源 ID、起止时间与耗时；依赖失败时标记 skipped。证据账本区别 source、self_report、safety_rule。来源证据必须对应当前资料的精确连续片段和位置，自述标注未经核实。completed 表示该任务执行完成，不表示病情安全或信息已确认。

## 异步接口

修改请求仍需当前访客 Cookie、CSRF Token 与合法 Origin，跨访客资源返回 404。

| 接口 | 请求 / 返回 |
| --- | --- |
| POST /api/visits/{visit_id}/tasks | {goal, use_model?:false} → 202 {task: TaskJob} |
| GET /api/visits/{visit_id}/tasks | {items: TaskJob[]} |
| GET /api/visits/{visit_id}/tasks/{task_id} | TaskJob |
| POST /api/visits/{visit_id}/tasks/{task_id}/apply | {} → {task, visit}，需本人确认 |
| GET /api/tasks/audit | 当前访客的任务操作审计及完整性结果 |

TaskJob 包括 id、visit_id、goal、status、created_at、updated_at、source_revision、applied_at、result、error，以及当前可用性字段 current_risk、invalidated_reason、can_apply。

| status | 含义 |
| --- | --- |
| queued / running | 已接受等待 / 正在执行 |
| completed | 工具任务完成，是否可保存看 can_apply |
| needs_input | 还需补充内容，可能有部分准备草稿 |
| emergency | 当前需要优先获得人类帮助，暂停使用准备结果 |
| failed | 执行未完成，原始资料未修改 |
| interrupted | 服务停止造成中断，需重新运行 |
| superseded | 启动后原始资料变化，旧结果不能保存 |

并发限制为最多两项同时执行、八项运行或等待；同一记录只允许一个活动任务，单记录最多保留五十项。读取任务时根据当前记录重新判断可保存性，不修改历史工具输出来伪装新执行。记录后来出现紧急表现，即使原任务完成，也显示紧急状态并禁止 apply。

apply 在事务内检查当前风险、记录修订、知识版本与已应用标记。成功只保存摘要、清单与准备计划；重复应用、资料过期、知识版本变化或紧急状态返回 409。已删除记录不会被慢任务重建。启动将未完成任务标记中断，不自动重放旧外部请求。

## 数据和资料版本

任务结果 payload 使用 Fernet，加密内容绑定访客、任务与记录 ID；损坏密文或错误绑定不继续使用。审计存操作元数据，不存患者正文；HMAC 链与链头校验能发现本机记录修改与截断，但不构成外部不可篡改审计。

访视记录、聊天与会话元数据仍未加密。任务密钥保存在本机数据目录，与数据库在同一主机；需要保护和备份密钥，没有 KMS 或整库加密，不能声称达到真实患者系统的数据保护要求。清理删除当前应用数据，不保证清除导出件、系统残留和外部备份。

当前资料库共 46 篇。scripts/knowledge_versions.py 提供 snapshot、list 和 rollback <hash>，验证 Source schema 和规范化内容哈希；回滚保留当前有效版本的快照。此操作由本机维护者执行，Agent 没有修改或回滚工具。任务结果记录 knowledge_version，保存时必须匹配现有资料库。

## 安全前置与可选语义复核

确定性规则先处理当前表述，覆盖有限的心肺、卒中样表现、严重过敏、意识变化和新增九类警示场景；聊天、表单与目标任务均不得跳过安全检查。不同人群、症状组合与及时联系专业人员的边界见 [红旗依据](redflag-evidence.md)，规则命中不是确诊，规则未命中不是安全证明。

自伤意念走独立陪伴与求助分支，不继续普通表单追问，不提供危险方法。地区心理资源只在适用地区核实后显示，不能把境外号码用于其他地区。

LocalSafetyReview 默认关闭，配置 LOCAL_SAFETY_ENABLED 与 LOCAL_SAFETY_MODEL。启用后固定连接本机 Ollama；不得使用外部模型、环境代理或重定向发送当前输入。模型只返回限定类别、紧急程度、时间上下文和原文证据，不返回诊断或处置方案。只有明确的当前紧急分类且连续证据通过校验时才升级警示；否定、历史、假设、不确定、超时及无效结构保持原规则结果。已命中的紧急规则直接返回，不等待模型。

GET /api/bootstrap 的 safety_review 显示 enabled、available、model、mode 和说明；模式为 rules_only、rules_and_local_review 或 rules_local_review_unavailable。可用状态仅表示服务状态，当前未完成真实本机语义模型验收或临床评测，不能把它描述成已验证的第二道医疗防线。

## 验证的解释

规则回归应包含独立自然语言、误拼写、否定、旧病史、科普、特殊人群与边界案例，并分别验证聊天和表单入口；任务检查需要覆盖紧急短路、资料变化、版本变化、幂等与实际写入。模拟模型用于校验结构和数据边界，不能代替真实模型有效性验证。

测试通过只说明固定案例中的工程行为符合预期，不代表临床召回率、诊断准确率、医疗认证或完全没有遗漏。本项目没有实际患者验证、临床审核、医疗合规认证或 Kubernetes 部署验收。