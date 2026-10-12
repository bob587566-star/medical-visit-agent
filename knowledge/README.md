# 资料说明

articles.json 是根据官方患者教育页面整理的简短中文释义，不是临床指南全文，也未经过临床专家审校。来源链接保留；reviewed_at 表示本项目核对网页的日期，source_reviewed_at（如有）是原网页显示的审阅日期。核对日期不表示已获医疗机构认证。

NHS 原文面向英国患者，急救联系方式按中国大陆国家卫健委公开资料单独说明，其他地区使用当地号码。医院要求、科室挂号、医保及诊疗方案不从英国资料推导。资料不包含药物处方或个人剂量建议。

当前共 46 篇资料，分为就诊准备、健康资料、紧急提醒、慢病随访、特殊人群、检查准备六类。V2 的 37 篇包含新增的 24 个独立主题；2026-10-12 针对独立验收报告补充 9 篇紧急求助和心理危机资料，复用原有晕厥主题。它仍是有限范围的患者教育库，不能覆盖全部急症；没有命中规则不能推出安全。病例和示例均为虚构。新增主题必须核对可信来源，不能用大模型无依据扩写入库。

## 检索如何工作

`backend/knowledge.py` 保留同步检索：人工整理主题与中文表达，确认问题有相应资料，再用 BM25 排序。没有支持的医院地址、医保、费用或药物剂量问题不会因词语重叠而获得“答案”。儿童发热会排除成人发热资料，避免直接套用成人建议。

`backend/retrieval.py` 增加本机 Ollama 混合检索。按 [Ollama 官方 Embedding 接口](https://docs.ollama.com/api/embed) 调用固定 `http://127.0.0.1:11434/api/embed`，默认模型 `bge-m3:latest`；先核对安装状态与实际嵌入返回值，再将主题候选的余弦相似度排序和 BM25 排序作加权 RRF 融合。向量仅用于资料排序，不是疾病概率或医疗置信度；语义排序也不能引入未经主题支持的资料。

资料文本、查询的嵌入请求仅指向本机 Ollama，禁止 HTTP 重定向；宿主 HTTP 客户端使用 `trust_env=False`，避免环境代理转发。本模块不会向第三方 Embedding 服务发送查询。磁盘缓存仅保存公开资料的 ID 和向量，不缓存患者问题；资料完整内容哈希、模型名称和模型摘要共同控制失效，资料正文修改后不会继续使用旧向量。

运行状态明确区分 `hybrid_ollama` 和 `lexical_fallback`。Ollama 未启动、模型未安装、超时或返回不合规范时，会透明回退到主题/BM25 检索，而不是假装语义服务已运行。当前机器 Ollama 未启动，本机服务验收应记录为词法回退；混合检索路径用本机接口模拟覆盖，不应写成真实嵌入服务验收通过。

主要来源：

- [MedlinePlus：与医生沟通](https://medlineplus.gov/talkingwithyourdoctor.html)
- [MedlinePlus：就诊准备](https://medlineplus.gov/ency/patientinstructions/000860.htm)
- [NHS：胸痛](https://www.nhs.uk/symptoms/chest-pain/)
- [NHS：呼吸困难](https://www.nhs.uk/symptoms/shortness-of-breath/)
- [NHS：卒中表现](https://www.nhs.uk/conditions/stroke/symptoms/)
- [NHS：严重过敏](https://www.nhs.uk/conditions/anaphylaxis/)
- [NHS：咳嗽](https://www.nhs.uk/symptoms/cough/)
- [NHS：成人发热](https://www.nhs.uk/symptoms/fever-in-adults/)
- [NHS：头痛](https://www.nhs.uk/symptoms/headaches/)
- [NHS：腹泻与呕吐](https://www.nhs.uk/symptoms/diarrhoea-and-vomiting/)
- [国家卫健委：科学就医健康教育核心信息](https://www.nhc.gov.cn/xcs/c100122/201411/32c1bd8c5b13436ba9ec83ef1834856e.shtml)

## V2 新增主题与核对来源

| 主题 | 官方原文 | 原页面审阅日期（如显示） |
| --- | --- | --- |
| 喉咙不适 | [NHS：Sore throat](https://www.nhs.uk/symptoms/sore-throat/) | 2024-04-08 |
| 腰背不适 | [NHS：Back pain](https://www.nhs.uk/conditions/back-pain/) | 2026-03-05 |
| 头晕 | [NHS：Dizziness](https://www.nhs.uk/symptoms/dizziness/) | 2023-04-21 |
| 持续疲倦 | [NHS：Tiredness and fatigue](https://www.nhs.uk/symptoms/tiredness-and-fatigue/) | 2023-06-02 |
| 睡眠困扰 | [NHS：Insomnia](https://www.nhs.uk/conditions/insomnia/) | 2024-03-19 |
| 腹部不适 | [NHS：Stomach ache](https://www.nhs.uk/symptoms/stomach-ache/) | 未列入本库 |
| 排便困扰 | [NHS：Constipation](https://www.nhs.uk/conditions/constipation/) | 2023-10-26 |
| 排尿不适 | [NHS：Urinary tract infections](https://www.nhs.uk/conditions/urinary-tract-infections-utis/) | 2025-07-11 |
| 饮水和尿量变化 | [NHS：Dehydration](https://www.nhs.uk/conditions/dehydration/) | 2026-05-01 |
| 血压随访 | [NHS：High blood pressure](https://www.nhs.uk/conditions/high-blood-pressure/) | 2024-07-19 |
| 血糖与糖尿病沟通 | [NHS：Type 2 diabetes symptoms](https://www.nhs.uk/conditions/type-2-diabetes/symptoms/) | 2025-02-10 |
| 心跳不适 | [NHS：Heart palpitations](https://www.nhs.uk/symptoms/heart-palpitations/) | 2026-03-17 |
| 接触后皮肤不适 | [NHS：Contact dermatitis](https://www.nhs.uk/conditions/contact-dermatitis/) | 2023-05-03 |
| 季节性鼻眼困扰 | [NHS：Hay fever](https://www.nhs.uk/conditions/hay-fever/) | 2024-03-21 |
| 耳部疼痛 | [NHS：Earache](https://www.nhs.uk/symptoms/earache/) | 2025-10-27 |
| 牙齿疼痛 | [NHS：Toothache](https://www.nhs.uk/symptoms/toothache/) | 2024-07-01 |
| 眼睛发红 | [NHS：Red eye](https://www.nhs.uk/symptoms/red-eye/) | 2025-10-27 |
| 运动损伤 | [NHS：Sprains and strains](https://www.nhs.uk/conditions/sprains-and-strains/) | 2024-04-23 |
| 短暂晕厥 | [NHS：Fainting](https://www.nhs.uk/symptoms/fainting/) | 2026-08-17 |
| 鼻出血 | [NHS：Nosebleed](https://www.nhs.uk/conditions/nosebleed/) | 2023-12-05 |
| 儿童发热 | [NHS：Fever in children](https://www.nhs.uk/symptoms/fever-in-children/) | 2024-01-03 |
| 孕期就诊 | [NHS：Antenatal care](https://www.nhs.uk/pregnancy/your-pregnancy-care/your-antenatal-care-and-appointments/) | 2026-04-17 |
| 抽血前准备 | [MedlinePlus：Fasting for a blood test](https://medlineplus.gov/lab-tests/fasting-for-a-blood-test/) | 不将引文更新时间作正文审阅日期 |
| 理解化验结果 | [MedlinePlus：How to understand your lab results](https://medlineplus.gov/lab-tests/how-to-understand-your-lab-results/) | 不将页面更新时间作临床审阅日期 |

新增正文为围绕记录、沟通和寻求专业帮助的简短中文释义。没有搬运原文全文，没有药物剂量、个人诊疗方案、医疗机构实时数据或医保规则。

## 红旗补充资料与边界

下列原页面于 2026-10-12 实际打开核对，新增正文仍为简短中文释义，未经临床专家审校。适用人群、紧急升级边界与来源核对说明见 [红旗依据](../docs/redflag-evidence.md)。来源描述的紧急求助等级与产品采取的保守警示需区分，不能因为规则命中而确认某种疾病。

| 资料 ID | 官方原文 | 原页面审阅日期（如显示） |
| --- | --- | --- |
| crisis_support | [NIMH：Suicide FAQ](https://www.nimh.nih.gov/health/publications/suicide-faq) | 修订年份 2023，未作完整日期入库 |
| poisoning | [NHS：Poisoning](https://www.nhs.uk/conditions/poisoning/) | 2025-06-12 |
| seizure | [NHS：Seizure first aid](https://www.nhs.uk/symptoms/what-to-do-if-someone-has-a-seizure-fit/) | 2023-12-19 |
| severe_haemoptysis | [NHS：Coughing up blood](https://www.nhs.uk/symptoms/coughing-up-blood/) | 2024-06-13 |
| severe_bleeding | [NHS：Cuts and grazes](https://www.nhs.uk/conditions/cuts-and-grazes/) | 2026-04-02 |
| pregnancy_bleeding | [NHS：Vaginal bleeding in pregnancy](https://www.nhs.uk/pregnancy/common-symptoms/vaginal-bleeding/) | 2026-02-17 |
| infant_warning | [NHS：Fever in children](https://www.nhs.uk/symptoms/fever-in-children/) | 2024-01-03 |
| head_injury | [NHS：Head injury and concussion](https://www.nhs.uk/conditions/head-injury-and-concussion/) | 2025-05-29 |
| vomiting_blood | [NHS：Vomiting blood](https://www.nhs.uk/symptoms/vomiting-blood/) | 2025-08-18 |

危机资料保留陪伴、倾听、减少危险物品可及和连接专业支持，不提供危险行为的做法，也不把网页上的境外心理热线号码套用到中国。心理热线需另行核实适用地区；本资料库没有经过该核实的中国心理热线号码。新增 NHS 紧急提醒使用“当地急救服务”，不直接搬用英国号码。
