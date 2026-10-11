# 知禾工作台 V2：实现接口约定

保留 V1 所有接口和访客/CSRF/显式模型开关，新增字段均有默认值，可读取旧记录。

## Visit 新增字段

- collection: {stage:'start'|'collecting'|'ready'|'urgent', completion:number(0..100), missing_fields:string[], next_question:string, captured_fields:string[]}
- intake_candidates: [{id,field,value,evidence,created_at}]。field 仅允许 Intake 的八个字段。候选只供本人确认，未确认不写表单、不当成已核实病情；evidence 必须来自实际自述。
- observations: [{id,kind:'symptom'|'temperature'|'blood_pressure'|'blood_glucose'|'note',value,unit,recorded_at,notes,created_at}]。均为本人记录，不解释测量结果、不计算疾病风险。
- plan: {generated_at,source:'self_report',goal,questions:string[],steps:[{id,title,detail,category}],notice}|null。就诊沟通和准备事项，无诊疗方案。

Message 新增 intake_candidates 同结构数组，可就地确认。Trace 增加 suggest_intake_updates / build_preparation_plan。分类名供展示，不能冒充模型隐式思维。

## 新接口

- GET /api/workspace → {visits_count,messages_count,observations_count,ready_count,knowledge_count,bookmarks_count,recent_visits:Visit[],retrieval:{mode,embedding_available,model,document_count},privacy:{retention_days,local_default}}。
- PATCH /api/visits/{id}/title {title} → Visit。
- POST /api/visits/{id}/candidates/{candidate_id}/confirm {} → Visit；不存在或已经失效404。若填写已有值，只在明确确认后覆盖。
- POST /api/visits/{id}/observations {kind,value,unit?:'',recorded_at,notes?:''} → Visit。
- DELETE /api/visits/{id}/observations/{observation_id} → Visit。
- POST /api/visits/{id}/plan {} → {plan,visit}。
- GET /api/library?q=&category=&bookmarked=false → {items:Source[],categories:string[],bookmark_ids:string[],total:number,retrieval:{mode,embedding_available,model,document_count}}。搜索在本机，真实语义服务不可用时明确标明主题/词法回退。
- PUT /api/library/bookmarks/{source_id} {saved:boolean} → {bookmark_ids:string[]}，CSRF 必须。
- GET /api/library/{source_id} → Source。未知返回404。
- GET /api/library/evidence?q=... → {items:Source[],retrieval:{...}}，检索解释仅返回真实执行状态，不将分数称为医疗置信度。

临床正文仍来自核对过的教育资料。Embedding 若启用仅用本机 Ollama；不把患者查询发到第三方嵌入服务。没有支持资料时不拼凑；医疗专业审核尚未完成。
