# 存储与运行工程

这些能力用来减少本机演示和后续部署的工程风险，不代表医疗机构资质、备案、临床审核或高可用已经完成。真实患者部署仍需按实际业务、机构与地域要求审核。

## 数据库版本

`backend/migrations.py` 按顺序登记版本、名称、SQL 校验和与应用时间，初始化及 `scripts/migrate.py` 共用同一实现。v1 是原访客／记录基线，v2 是加密元数据、账号、同意及任务表，v3 是任务输入、事务 outbox 与执行 fencing。已应用版本不修改；后续变更追加下一版。旧库只在结构兼容时收编；不同校验和、缺失历史或未来版本会拒绝启动。DDL 在事务／savepoint 中执行。

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py status --db data\visits.sqlite3
```

迁移登记不等于已完成任意 SQLite→PostgreSQL 数据搬迁。既有记录的搬迁须另行校验归属、密钥、行数、同意与回滚路径。不要把 PostgreSQL URL 当作 SQLite 文件传给工具。

## SQLite 备份与恢复

`scripts/backup.py` 使用 SQLite online backup 得到已提交数据的一致快照，包含 WAL 中的提交，而非复制单个正在写入的数据库文件。数据库与所需的记录密钥、任务密钥先在内存封装，再用独立备份口令通过 Scrypt 派生密钥及 Fernet 认证加密。工具不打印口令、密钥或病历。旧明文库也可先创建加密归档，再执行加密升级。

备份核对 SQLite 完整性、外键、记录／CSRF 密文上下文、任务绑定及任务审计；扩展表中的版本化密文也认证。外部／环境密钥必须同时传入或配置。密钥若在快照期间变化，会拒绝发布归档。

```powershell
.\.venv\Scripts\python.exe scripts\backup.py backup --db data\visits.sqlite3 --out data\backups\before-change.zhihe --password-file data\backup-password.private
.\.venv\Scripts\python.exe scripts\backup.py verify --backup data\backups\before-change.zhihe --password-file data\backup-password.private
.\.venv\Scripts\python.exe scripts\backup.py restore --backup data\backups\before-change.zhihe --target data\restore-drill\visits.sqlite3 --password-file data\backup-password.private
```

口令文件和输出目录须提前准备并私密保管。恢复只写新的离线目标，遇到已有数据库、WAL 或密钥即拒绝覆盖。先验证恢复后的记录、账号、同意、任务和资料版本，再停服务切换。外部密钥恢复成目标目录内自包含密钥文件；切换时必须检查密钥配置。不要遗失归档口令，也不要把备份口令与数据库密钥一起公开。单个数据库上限 128 MiB；更大的系统应选专用备份方案。

本机密钥文件与加密数据库分离，能降低仅泄漏数据库副本的风险。能读取同主机全部密钥的管理员或被攻陷进程仍可解密，这不等于云 KMS。工具完成不等于已经设置定时备份、异地保存或定期恢复演练。撤回／删除在线数据也不会自动擦除已有备份；运营方须另定备份保留与删除政策。

## PostgreSQL 和 Redis 验收环境

`compose.production.yaml` 提供隔离的工程 PostgreSQL／Redis 服务，以及 `application` profile 的 API、独立 worker 和 Prometheus。数据服务仅监听本机 `18432`／`18379`，application 页面为 `8273`，Prometheus 为 `19090`；官方镜像固定 digest。与既有客服、RAG 和 SQLite 数据卷分开。`configure_runtime_check.py` 生成独立口令与 Redis 配置，放在 Git 忽略的数据目录，不打印连接 URL。

```powershell
.\.venv\Scripts\python.exe scripts\configure_runtime_check.py
.\.venv\Scripts\python.exe scripts\configure_runtime_containers.py
docker compose -p zhihe-engineering-check -f compose.production.yaml config --quiet
docker compose -p zhihe-engineering-check -f compose.production.yaml up -d postgres redis
.\.venv\Scripts\python.exe scripts\check_redis_runtime.py
```

Redis 用密码保护、AOF、`everysec` 同步及 `noeviction`。断电仍可能损失最近约一秒的 Redis 写入；应用事务 outbox 用于恢复数据库已保存而尚未投递的任务。Streams 为至少一次投递，独立 worker 必须在数据库提交前校验版本与 fencing，提交后才 ack；Redis lease 本身不是数据库事务。坏队列内容和共享限流故障不能静默退回“看起来可用”的进程内实现。

共享限流使用 Redis `TIME` 和原子 Lua 滑窗。队列只放服务器产生的不透明任务 ID；健康正文、用户身份、Cookie 和模型密钥不放入 Redis。公共资料 embedding 缓存与个人数据仍有不同用途和生命周期。

PostgreSQL 模式提供连接池和数据库事务，但当前仍是单机 PostgreSQL／Redis；启动更多容器不等于主从复制、跨区容灾或零丢失。数据库长事务、模型成本、锁等待与故障恢复需持续测量。

`scripts/postgres_backup.py` 专门服务这套隔离 fixture：在导出的只读一致快照下执行 `pg_dump`，把 dump 和密钥放入认证加密归档；恢复仅生成一个新 `zhihe_restore_*` 数据库，拒绝覆盖原库，再验证密文、任务审计及行数。它不是通用生产 PostgreSQL 运维工具，也没有配置远端存储或 PITR。真实生产需独立的 `pg_dump`／base backup、WAL 归档及恢复演练策略。

## 观测和有限负载

`attach_observability` 提供 Prometheus 文本格式请求计数与耗时直方图，标签仅有 HTTP 方法、框架注册的路径模板和状态码。未匹配路径统一归为 `unmatched`，不会把记录 ID 或查询词当成标签。`/metrics` 默认仅限实际 loopback 客户端；配置 `METRICS_TOKEN` 后须用 Bearer token，不能用伪造 `X-Forwarded-For` 获取指标。

结构化日志只记路径模板、方法、状态、耗时和已校验的请求 ID，默认 INFO 级别单个 JSON StreamHandler，不记录正文、查询参数、IP、Cookie、账号或 Key。Compose 平台日志配置每文件 10 MiB、最多 3 个；本机启动重定向若长期运行也需配置日志轮转。这里尚未完成 OTel 端到端 tracing、外部错误聚合或告警通知。

`scripts/load_test.py` 限制为 loopback、最多 12 名虚构访客、每人 5 轮、并发最多 8。聊天场景单独同意本机留存，明确关闭外部 AI，结束删除本次记录并撤回本次同意；报告只输出请求数、状态、p50／p95 和耗时。该小场景不证明最大容量、真实用户 SLA、模型推理吞吐或临床安全性。

## 依赖与 CI 门禁

`.github/workflows/ci.yml` 为只读权限的 push／PR 流水线，包含后端回归、模型边界模拟、依赖审计、页面测试与构建、独立 Edge 浏览器和容器 readiness；Actions 已固定到核对过的官方 commit，不自动发布或部署。新增已知漏洞会让审计步骤失败。

2026-10-13 实际执行的 pip-audit 与 npm audit 报告保存在 `artifacts/dependency-audit-*-20261013.json`。发现旧 Starlette／pytest／Vue／Vite／Vitest 依赖风险后升级；复审当前安装依赖返回 0 个已知漏洞。0 个公告不等于没有未知漏洞，也不能替代完整渗透测试。`check_repository_secrets.py` 只是已知私密路径和明显密钥格式检查，不是完整 secret scanner。

参考实现依据：[SQLite online backup](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup)、[WAL 快照的 deserialize 限制](https://www.sqlite.org/c3ref/deserialize.html)、[Redis XAUTOCLAIM](https://redis.io/docs/latest/commands/xautoclaim/)、[pip-audit](https://pypi.org/project/pip-audit/)。

本机、原 Docker、CLI 验收和 application 容器的入口及 Cookie 关系见 [安全与部署入口](security-and-operations.md)。已有密钥缺失时拒绝自动替换。
