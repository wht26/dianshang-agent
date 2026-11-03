# PostgreSQL + Redis（v0.10）

## 实际职责

PostgreSQL 17保存聊天会话、问答历史、主动保存的偏好、店铺配置、导入的知识库正文、SKU库存、订单、物流事件、退款与审计记录。业务金额保持整数分；库存与退款金额占用使用数据库事务和事务级咨询锁串行核验。Redis不是资金台账。

Redis 7保存带TTL的请求限流计数与问答租约。多个应用进程共享限流和生成互斥；租约定期续期，释放时核验随机所有权令牌，服务失效会拒绝继续问答，不降级为进程内锁。当前仍是单店铺、全局一轮生成，不是每位顾客独立并发。默认按直接连接IP每60秒60次；反向代理部署需另外设计可信代理和顾客身份限流。

知识检索仍是BM25；数据库迁移不会把它变成向量检索。跨进程在下一次知识列表、bootstrap或问答请求时从PostgreSQL刷新店铺与导入知识。

## Windows本机启动

需要Docker Desktop的Linux引擎正常运行，以及Python 3.11+。在项目目录的PowerShell执行：

```powershell
. ./setup-storage.ps1
docker compose up -d postgres redis
pip install -e ".[dev]"
./start.ps1
```

setup脚本生成随机数据库/Redis密码，写入Windows用户级环境变量并加载到当前进程，不创建密钥文件、不打印密码。重复运行沿用用户级密码。它为此本机Compose生成DATABASE_URL和REDIS_URL；如果使用托管服务，应自行配置对应URL，不运行该本机初始化脚本覆盖它们。

PostgreSQL映射127.0.0.1:15432，Redis映射127.0.0.1:16379，页面127.0.0.1:8000。已有8000端口应用需先停止。数据存入独立Docker命名卷，普通容器重启不清除数据。不要执行 `docker compose down -v`，它会删除数据卷。

如果Docker报告WSL磁盘文件缺失，先修复Docker Desktop运行环境，或连接已有PostgreSQL/Redis。项目不会自动重置WSL或删除Docker数据。

启动命令改为应用工厂，旧 `wang.app:app` 不再使用：

```powershell
python -m uvicorn wang.app:create_app --factory --host 127.0.0.1 --port 8000
```

## 全容器启动

```powershell
. ./setup-storage.ps1
$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
$env:WANG_ADMIN_TOKEN = [Environment]::GetEnvironmentVariable('WANG_ADMIN_TOKEN', 'User')
docker compose up -d --build
```

Compose将应用容器连接到内部postgres/redis主机，主机开发命令使用本机端口URL。商家网关配置仍通过OS环境变量提供。其他系统也可使用Compose，需自行安全设置两个WANG_*_PASSWORD及模型/商家环境变量；密码用随机十六进制等URL安全字符，避免插入URL时产生歧义。`.env.example`仅为字段说明，不包含真实凭证；Python应用不自动读取`.env`。

`GET /health`真实探测PostgreSQL和Redis，正常返回 `database=postgresql`、`coordination=redis`；不可用返回503。不配置DATABASE_URL/REDIS_URL会拒绝启动，不会悄悄回退SQLite。可用托管数据库的TLS URL与 `rediss://` 接入托管Redis。

## 迁移旧SQLite

先备份旧数据库，停止所有旧版应用写入；仅启动两个依赖服务，暂不打开新应用/页面。目标必须是空的业务表（可预建表），包括不能有新页面生成的会话。执行：

```powershell
. ./setup-storage.ps1
docker compose up -d postgres redis
python -m wang.migrate_sqlite ./data/wang-support.db
./start.ps1
```

工具只读打开旧文件，通过SQLite backup生成一致性内存快照，将已知业务表在一个PostgreSQL事务中导入，保留ID和访问凭证摘要并修复自增序列。失败会回滚插入；目标已有任意业务记录时拒绝覆盖。源文件不改动，不把数据上传GitHub。只输出各表数量，不输出顾客内容或密钥。旧库不自动导入，不合并两套已有数据；保留源文件直到人工核验完成。

浏览器原Cookie仍可使用，因为摘要与会话ID保留；记忆Cookie过期、清除或换设备依然不支持账号同步。外置偏好JSON导入导出保持原协议。

## 集成测试

GitHub Actions在Python 3.11/3.13上启动真实PostgreSQL 17、Redis 7。订单防超卖、退款金额并发占用、持久化、租约所有权、跨实例限流、Redis故障与旧库迁移均使用真实服务；外部商家网关与模型仍使用隔离的模拟HTTP响应。

本机请准备**专用测试库**，用户需具有创建schema权限。fixture为每例建立随机schema并在结束后删除，只清理该测试的Redis随机前缀，不FLUSHDB。历史测试传入的 `tmp_path/*.db` 在fixture中仅作为schema命名空间标识，不使用SQLite运行应用。迁移测试才创建临时SQLite源文件。

```powershell
# 在本机Compose中新建专用测试库（首次执行）
docker compose exec postgres createdb -U wang wang_test
$env:TEST_DATABASE_URL = $env:DATABASE_URL -replace '/wang$', '/wang_test'
$env:TEST_REDIS_URL = $env:REDIS_URL
python -m pytest -q
ruff check .
node --check wang/static/app.js
```

不提供测试服务URL时测试明确失败，避免把缺少数据库误报成通过。正式部署还需要数据库备份恢复、独立管理权限、可信顾客身份与租户隔离；此次变更没有新增这些能力。
