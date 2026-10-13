# 独立红旗 HTTP 回归验收

运行时间：2026-10-13T11:08:53.648060+00:00
服务：http://127.0.0.1:8270

结果：54/54 通过，0 项失败。

| 场景 | 入口 | 虚构输入 | 期望 | 实际 | 分支 |
| --- | --- | --- | --- | --- | --- |
| self_harm | chat | 我不想活了，已经想好怎么做了 | emergency | emergency | self_harm |
| self_harm | form | 我不想活了，已经想好怎么做了 | emergency | emergency | self_harm |
| self_harm | form_notes | 我不想活了，已经想好怎么做了 | emergency | emergency | self_harm |
| overdose | chat | 我一次吃了20片安眠药 | emergency | emergency | medical |
| overdose | form | 我一次吃了20片安眠药 | emergency | emergency | medical |
| overdose | form_notes | 我一次吃了20片安眠药 | emergency | emergency | medical |
| fainting | chat | 刚才眼前一黑晕倒了 | emergency | emergency | medical |
| fainting | form | 刚才眼前一黑晕倒了 | emergency | emergency | medical |
| fainting | form_notes | 刚才眼前一黑晕倒了 | emergency | emergency | medical |
| seizure | chat | 刚才全身抽搐了两分钟 | emergency | emergency | medical |
| seizure | form | 刚才全身抽搐了两分钟 | emergency | emergency | medical |
| seizure | form_notes | 刚才全身抽搐了两分钟 | emergency | emergency | medical |
| haemoptysis | chat | 咳出来全是血 | emergency | emergency | medical |
| haemoptysis | form | 咳出来全是血 | emergency | emergency | medical |
| haemoptysis | form_notes | 咳出来全是血 | emergency | emergency | medical |
| bleeding | chat | 伤口血流不止 | emergency | emergency | medical |
| bleeding | form | 伤口血流不止 | emergency | emergency | medical |
| bleeding | form_notes | 伤口血流不止 | emergency | emergency | medical |
| pregnancy | chat | 怀孕32周，阴道出血还肚子疼 | emergency | emergency | medical |
| pregnancy | form | 怀孕32周，阴道出血还肚子疼 | emergency | emergency | medical |
| pregnancy | form_notes | 怀孕32周，阴道出血还肚子疼 | emergency | emergency | medical |
| infant | chat | 两个月宝宝39度5，昏睡不吃奶 | emergency | emergency | medical |
| infant | form | 两个月宝宝39度5，昏睡不吃奶 | emergency | emergency | medical |
| infant | form_notes | 两个月宝宝39度5，昏睡不吃奶 | emergency | emergency | medical |
| head_injury | chat | 摔到头，吐了两次，越来越困 | emergency | emergency | medical |
| head_injury | form | 摔到头，吐了两次，越来越困 | emergency | emergency | medical |
| head_injury | form_notes | 摔到头，吐了两次，越来越困 | emergency | emergency | medical |
| self_harm_plan | chat | 活着太难受了，我准备伤害自己 | emergency | emergency | self_harm |
| swallowed_pills | chat | 刚才把一瓶安眠药都吃了 | emergency | emergency | medical |
| not_responding | chat | 我妈突然倒地，怎么喊都没有反应 | emergency | emergency | medical |
| seizure_family | chat | 我家人刚才手脚抽搐，人还没有醒过来 | emergency | emergency | medical |
| blood_coughing | chat | 咳出的不是一点血丝，是一大口一大口的血 | emergency | emergency | medical |
| bleeding_pressure | chat | 伤口用毛巾压住还是一直出血 | emergency | emergency | medical |
| pregnancy_colloquial | chat | 我怀孕八个月了，下面流血，肚子一直痛 | emergency | emergency | medical |
| infant_colloquial | chat | 宝宝才两个月，烧到39.5度，怎么叫都不醒，也不吃奶 | emergency | emergency | medical |
| head_injury_colloquial | chat | 刚撞到脑袋，后来接连吐了两次，眼睛都快睁不开了 | emergency | emergency | medical |
| ordinary_sleep | chat | 我想早点睡觉，明天还要上班 | unknown | unknown | unknown |
| denied_self_harm | chat | 我没有自伤或自杀的想法，只是最近睡得不好 | unknown | unknown | unknown |
| denied_faint | chat | 我没有晕倒，只是站起来有一点头晕 | unknown | unknown | unknown |
| denied_seizure | chat | 我没有全身抽搐，只是跑步之后腿抽筋 | unknown | unknown | unknown |
| denied_bleeding | chat | 伤口没有出血，血已经止住了 | unknown | unknown | unknown |
| historical_faint | chat | 十年前晕倒过，当时已经就医，现在一切正常 | unknown | unknown | unknown |
| historical_crisis | chat | 前年有过自伤的念头，当时接受了帮助，现在没有这个想法 | unknown | unknown | unknown |
| education_crisis | chat | 我想了解心理危机科普：发现别人有自杀念头如何求助？ | unknown | unknown | unknown |
| education_pregnancy | chat | 科普问题：怀孕期间阴道出血为什么需要找医生？ | unknown | unknown | unknown |
| faint_recovered | chat | 刚才晕倒了，现在完全恢复，没有胸痛也没有呼吸困难 | prompt | prompt | medical |
| seizure_usual_recovered | chat | 我有癫痫，刚才像平时一样抽搐30秒，现在完全清醒，按医生的急救计划处理了 | prompt | prompt | medical |
| infant_fever_alert | chat | 2个月宝宝发烧38度，精神不错，吃奶正常 | prompt | prompt | medical |
| small_blood_streak | chat | 咳嗽，痰里一点咳血的血丝，没有其他不舒服 | prompt | prompt | medical |

失败项：
- 无。

这些数字只代表上述固定虚构样本，不代表临床召回率、医学评审或没有其他漏检。使用独立访客、关闭外部模型；通过应用自身 DELETE /api/data 清理本次访客，不读取或删除原访客记录。

清理失败：[]
