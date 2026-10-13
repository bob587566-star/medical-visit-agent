# 贡献说明

使用虚构记录复现问题。Issue、截图、日志和提交中不要包含真实健康资料、账号恢复码、Cookie、API Key、密码或备份文件。

安装方式见 README。修改后运行与改动相关的测试；提交前至少检查：

```powershell
.\.venv\Scripts\python.exe scripts/check_repository_secrets.py --history
git diff --check
```

后端测试使用独立临时库，不操作本机已有记录：

```powershell
New-Item -ItemType Directory -Force .pytest-tmp | Out-Null
.\.venv\Scripts\python.exe -m pytest backend/tests tests -q --basetemp=.pytest-tmp\contribution
cd frontend
npm test
npm run build
```

PostgreSQL／Redis 集成测试必须指定专用虚构测试环境，不能连接正式数据库。外部模型调用不是普通测试的前提；未经对应用户单独同意，不发送健康自述。

涉及警示规则、模型输出或资料库的修改，应同时说明来源与适用边界，并添加自然语言差异用例。固定测试通过不能作为临床有效性证明。
