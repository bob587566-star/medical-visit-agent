# 安全与部署入口

知禾用于就诊准备和健康资料学习；账号不是患者实名认证或医师执业身份。当前验收使用虚构资料，没有完成临床审校、备案／登记、等保或真实患者上线审核。

## 页面与数据环境

| 入口 | 运行方式 | 数据关系 |
| --- | --- | --- |
| `http://127.0.0.1:8270/` | Windows 本机服务 | 原 SQLite 数据库，原两条记录保留并迁移为密文 |
| `http://127.0.0.1:8271/` | 原 `compose.yaml` | 独立 SQLite 卷，须启动对应 Compose 服务 |
| `http://127.0.0.1:8272/` | `runtime_profile.py` 工程验收 | PostgreSQL／Redis，与 8273 同一隔离虚构数据库；用于 CLI worker 重启测试 |
| `http://127.0.0.1:8273/` | `compose.production.yaml` application profile | PostgreSQL／Redis、独立 worker；不自动导入原 SQLite 记录 |
| `http://127.0.0.1:19090/` | application profile 的 Prometheus | 请求计数、耗时和抓取状态；无健康正文 |

服务需要分别启动，列表不代表每个入口始终在线。SQLite 默认 Cookie 为 `zhihe_visitor`，PostgreSQL 默认使用 `zhihe_visitor_postgres`，避免 localhost 不同端口互相覆盖登录。其他同时运行的独立数据环境应配置不同 `VISITOR_COOKIE_NAME`，或使用不同主机名。8272 与 8273 数据和身份相同，不应当作两个独立商户。

## 日常使用

首次记录前单独选择本机留存，保存期限可选 1／7／30 天；外部 AI 同意另行选择，默认关闭。外部同意不等于自动发送，还需主动开启问答或任务的模型开关。服务在实际外发前再次检查授权，撤回后的未发送请求不继续发送；已经发给服务商的请求不能事后收回，其结果在撤回后不再保存。

可使用昵称、至少 15 字符的密码和一次性恢复码建立可找回的账号。恢复码只显示一次，服务端只存摘要；恢复会重设密码、换恢复码并撤销旧登录会话。注册绑定当前访客记录，登录其他账号不自动合并记录。未注册访客丢失 Cookie 后仍无法证明旧记录归属。

隐私面板支持本人 JSON 导出、撤回留存同意、密码确认注销。撤回删除当前身份的记录、聊天、任务及收藏，并保留最小同意事件；注销同时删除账号及同意事件。操作不会删除已下载文件或既有运维备份。

## 本机启动

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1
```

停止用 `scripts/stop.ps1`。该脚本核实本项目 Python 启动器及直接解释器子进程，再停止服务。安装用 `scripts/setup.ps1`，依赖受 lock 文件约束。

## 隔离容器环境

仅首次建立虚构验收环境时生成新的独立凭据与密钥；已有文件保留，缺失原密钥时拒绝重建。

```powershell
.\.venv\Scripts\python.exe scripts/configure_runtime_check.py
.\.venv\Scripts\python.exe scripts/configure_runtime_containers.py
docker compose -p zhihe-engineering-check -f compose.production.yaml --profile application config --quiet
docker compose -p zhihe-engineering-check -f compose.production.yaml --profile application up -d --build --wait
```

浏览器访问 8273；请求指标需要私密 Bearer token，Prometheus 通过 Secret 文件读取。接口与 worker 同用原密钥、数据库和队列；Redis 仅投递不透明任务编号，快照保存在加密 SQL 数据中。事务 outbox 补漏、至少一次投递、租约与提交 fencing 防止丢单和重复提交；当前仍是单机基础设施，不能认定高可用。

Linux 上宿主凭据文件保持 `0600`。一次性的 `secrets-init` 在无网络、只读根文件系统中，把原文件复制到两个私密命名卷：API／worker 使用 UID/GID `1000` 的记录与任务密钥，Prometheus 使用 UID/GID `65534` 的指标 token；文件均为 `0600`，目录为 `0700`。消费者只读挂载密钥卷，继续以非 root、只读根文件系统及移除全部 capabilities 运行。初始化只保留复制与设置归属所需的 `CHOWN`／`DAC_OVERRIDE`／`FOWNER`；不会生成新密钥，发现已有副本与原文件不同会拒绝启动。Redis 单独在容器内建立私密配置副本，再交给官方入口下降权限，宿主只读挂载不改权限。

命名卷中的密钥副本也需要私密保管。停机或普通 `down` 会保留数据与密钥卷；不要删除原密钥来重新生成。计划轮换时先停止消费者并执行对应密钥轮换流程，再同步专用密钥卷；不要把初始化拒绝覆盖当作可以重建数据库的理由。

在全新、只含虚构资料的 Linux CI checkout 中，可验证完整 profile；下列删除卷命令只适用于这次新建的 `zhihe-ci-application` 验收环境：

```bash
python scripts/configure_runtime_check.py
python scripts/configure_runtime_containers.py
docker compose -p zhihe-ci-application -f compose.production.yaml --profile application up -d --build --wait --wait-timeout 120
python deploy/check_application_runtime.py --project zhihe-ci-application
docker compose -p zhihe-ci-application -f compose.production.yaml --profile application down --volumes
```

CI 应在 `always()` 清理步骤执行最后一行。检查只访问固定 loopback readiness、需授权的指标和 Prometheus 抓取结果，并核对用户、文件权限和容器限制；不建立健康记录，也不调用外部 AI。

需要进程级验收时先启动 PostgreSQL／Redis，再运行 `scripts/runtime_profile.py start` 与 `tests/runtime_acceptance.py`；结束运行 `scripts/runtime_profile.py stop`。仅控制已核实的本项目验收进程，不操作 8270。运行重启验收前应停止 Docker worker，避免第二个 worker 接走测试刻意暂停的任务。

## 密钥、恢复与上线边界

账号昵称、健康正文、CSRF、同意和任务快照使用归属绑定的认证加密，ID／时间／索引仍是必要明文元数据。默认本机文件不是 KMS，能取得同机全部密钥的管理员仍可解密。部署可挂载独立密钥文件；不能缺失旧密钥后自动生成新密钥冒充原数据。

SQLite 有认证加密备份、新目标恢复和离线轮换工具；PostgreSQL 有本隔离 fixture 的一致快照备份与新数据库恢复验收工具。定时／异地备份、生产 WAL 归档、KMS 托管、OTel 全链路 tracing、告警通知、手机号／微信实名、医师认证以及临床审批仍需实际部署配置，未完成声明见 [生产工程说明](production-engineering.md) 和 [合规核对](compliance-deployment.md)。

Windows Docker Desktop 可能因残留 AF_UNIX 通信文件启动失败。保留原卷和密钥，核对 Docker 当前健康再执行容器验收；不能用历史成功报告代替当前可用状态。本机 8270 可独立运行。
