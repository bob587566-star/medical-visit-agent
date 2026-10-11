# 就诊准备 Agent：首版接口约定

开发目录相互独立：backend/、frontend/、knowledge/、scripts/、tests/。后端监听 127.0.0.1:8270，生产静态目录 frontend/dist。全部演示仅使用虚构病例。

## 身份与请求

GET /api/bootstrap 设置 HttpOnly、SameSite=Strict 的随机访客 Cookie，返回 csrf_token。所有修改请求带 X-CSRF-Token。仅允许访问本人访客的记录，越权返回 404，缺失会话返回 401。临床内容默认本机处理，调用外部模型须前端显式选择 use_model=true；不把 Cookie、身份或完整历史发到外部服务。

## 结构

Visit: {id,title,created_at,updated_at,intake,messages,summary,checklist,risk}

Intake: {chief_complaint,onset,age_group,severity,medications,allergies,medical_history,notes}，均为字符串。age_group: 未填写/成人/儿童/老年人/孕产期；severity: 未填写/轻/中/重。不收集姓名、电话、身份证或地址。未知字段不推测。

Message: {id,role,content,created_at,sources,trace,risk,suggested_questions,mode,duration_ms}。

Source: {id,title,publisher,url,reviewed_at,source_reviewed_at?:string|null,content,category,keywords,topics}。reviewed_at 为本项目核对网页日期；source_reviewed_at 为原网页日期（若有）。

Trace: [{tool,label,status}]，不显示模型隐式思维。

Risk: {level,message,reasons,source_ids}；level=emergency/prompt/unknown；未命中不能宣称安全或排除疾病。

Summary: {text,generated_at,source:"self_report"}，清晰标记自述和未填写。

Checklist: [{id,text,done}]。

## 接口

- GET /api/bootstrap → {csrf_token,model_available,privacy,sources,visits,current_visit}。visits 可为完整 Visit；无记录 current_visit=null。
- POST /api/visits {title?:string} → Visit。
- GET /api/visits/{id} → Visit。
- PATCH /api/visits/{id}/intake Intake → Visit。
- POST /api/visits/{id}/messages {content:string,use_model?:boolean} → {user_message,assistant_message,visit}。assistant_message 永不为空；无证据明确说明，不返回无关资料。
- POST /api/visits/{id}/summary {use_model?:boolean} → {summary,checklist,visit}。根据填表和自述生成，不写诊断或处方。
- PATCH /api/visits/{id}/checklist {item_id:string,done:boolean} → Visit。
- GET /api/visits/{id}/export → UTF-8 文本下载。无摘要时也能导出当前真实填报资料。
- DELETE /api/visits/{id} → 204。
- DELETE /api/data → 204，删除当前访客全部记录。
- GET /api/knowledge → {items:Source[]}。
- GET /healthz，GET /readyz。

## 医疗边界

先做紧急提醒规则检查，再查询人工整理的权威资料。严重呼吸困难、胸痛伴呼吸困难或冷汗、突发单侧无力或语言异常、喉舌肿胀伴呼吸困难、意识异常等命中后立即建议联系当地急救，不等待大模型；不提供药名剂量、不诊断。否定、一般资料提问、历史事件和当前本人症状要区分，不能用简单字符串出现就报警。任何用户输入不能关闭规则。提示不能覆盖所有紧急情况。

健康问答仅限知识库已有证据的教育与就诊准备内容；不凭排名分拼凑答案。未知主题明确无资料。儿童、孕产期等需专业人员确认。院址、预约和医保不可编造。

Agent 工具：check_urgent_signs → search_health_knowledge → collect_visit_details → build_visit_checklist → draft_visit_summary。必须存在实际受限执行器及输出校验，不把 trace 冒充工具执行。外部模型只补充已获支持的语义理解或摘要结构化字段；错误超时回退本机结果，不覆盖紧急提示、不输出诊断用药。
