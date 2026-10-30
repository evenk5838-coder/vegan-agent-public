# 蔬食内测部署包

本目录保留现有 Next.js、FastAPI、SQLAlchemy 与 PostgreSQL，提供生产部署准备，不代表已上线。当前完成了源码、配置与离线脚本自检，尚未拉取镜像、构建容器或验证 Linux 容器运行。部署者需自行准备云账号、服务器、地域、域名及预算，并核对所选环境的访问与备案要求。

默认拓扑为 Caddy → 同源前端及 API → 私有 PostgreSQL。只有 Caddy 发布80/443；数据库5432、API8000、网页3000均不发布。API进程内消费任务，所以固定一个 API 实例、一个 Uvicorn worker，不启用 reload 或自动横向扩容。此包不适用未经适配的 CloudBase REST 网关，也不将已有数据库改写成另一套存储。

## 文件与持久数据

- `Dockerfile.api`：沿用后端锁文件的运行依赖，排除本地开发的内嵌数据库与pytest。非root运行，先等待数据库，再运行Alembic，最后启动API。
- `Dockerfile.web`：Node22构建及 `next start`；不读取运行配置，不注入任何模型、地图或推送密钥。Caddy直接转发 `/api/v1/*`，既有 Next 本地 rewrite 不参与外部API流量。
- `compose.yaml`：PostgreSQL17、API、网页、Caddy2.10；镜像标签仍需首次构建时记录实际digest及依赖安装结果。
- `postgres_data`卷仅放数据库；`api_data`卷仅放私有原图和录音；Caddy证书和配置另有卷。网页与代理都不挂载API数据卷，也不配置静态文件服务。
- `Dockerfile.ops`与独立profile提供维护入口，使用PostgreSQL17原生工具和Python，不启动应用或供应商。`ops-backup`仅以只读方式挂载源媒体，写入独立 `ops_backups` 卷；`ops-restore`只读取备份卷，恢复到专属 `restore_postgres_data` 与 `restore_api_data` 卷。两者没有对外端口，也不会随普通 `up` 执行备份或恢复。
- 仓库根 `.dockerignore` 采用源文件白名单并排除 `.env`、Git、业务data、数据库、证书与日志。新增加的前端静态素材需要同步核对白名单。

## 运行配置

模板是 `runtime.env.example`。批准部署后，运维人员将它复制到项目目录外的 `/etc/shushi/runtime.env`，权限设为600，再私下填写；不要把密钥贴到聊天、命令参数、构建参数或前端环境变量中。

必填项目是已批准域名、证书通知邮箱、至少32位的随机独立数据库密码。模板保留空值，不能直接启动。`APP_ORIGIN` 固定由HTTPS域名生成，`COOKIE_SECURE=true`；禁止以HTTP地址承接登录或手机录音验收。高德控制台需另外核对该域名的允许范围，页面地图真实加载后才能记录验证通过。

`RELEASE_TAG` 用于本地私有API/网页镜像标识，每次已核验源码的构建使用新值并记录对应Git提交及镜像digest；不复用旧发布标签。Compose只在本机生成镜像，不会推送公开仓库。

`EXTERNAL_CALLS_ENABLED=false` 是默认值。配置凭据不等于获得真实调用费用授权；整体验收预算、首次真实调用和云资源费用需另外批准。模板保留保守调用额度，不自动增加已授权范围。数据库URL在容器启动时用结构化参数生成，密码中的特殊字符不会直接拼到URL里；该URL不打印。

配置文件只支持简单 `KEY=value` 或 `KEY='字面值'`。含 `$`、`#` 的值必须用单引号，禁止变量展开或双引号。当前终端若有同名环境变量，会覆盖Compose配置；预检发现不同值会拒绝继续。不要执行会输出运行配置全文的 `docker compose config` 或 `config --environment`。

## 批准资源后执行

以下是待授权后使用的操作说明，本次均未执行。

1. 在已批准的腾讯云资源准备Docker Engine与Compose，确认域名解析、80/443安全组和HTTPS条件；不要开放3000、8000、5432。
2. 将源码放到专属目录，将私有运行配置放在项目外。不要上传本机 `.env`、真实data、Git历史、审核素材或证据包充当镜像上下文。
3. 先离线检查：

```bash
python3 deploy/preflight.py --self-test
python3 deploy/preflight.py --env-file /etc/shushi/runtime.env
```

4. 获得部署与费用授权后才检查真实Compose配置、构建与启动：

```bash
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml config --quiet
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml build
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml up -d
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml ps
```

5. 核对HTTPS根页面、数据库就绪的健康接口、未登录401、两账号隔离、手机定位/录音/推送、重启恢复、日志脱敏与限额。**不把容器启动或健康200当作整体验收通过。**

健康脚本要求 `/api/v1/health` 同时返回 `status=ok`、`database=ok`、`environment=production`；数据库未就绪或旧版只回固定ok都视为不健康。此接口不公开worker数量、PID或数据库连接信息。Uvicorn不记录请求访问日志；Caddy不启用访问日志，避免保存私有路径及查询参数。实际部署后仍需核对平台额外采集的日志。

## 数据初始化、备份与回滚

镜像不带本机业务数据库、原图、录音或菜谱发布状态。新卷首次启动是空数据库。批量菜谱导入与发布继续按内容审核、来源与版本清单执行，容器启动不自动导入或发布。

数据库与私有文件必须作为一份有清单、SHA-256和校验结果的备份保存，离开服务器保存副本时需要另行确认存放位置。原图及音频属于私有数据，不能放到网页public目录或公开对象桶。备份不含运行密钥；运行配置需独立保管。禁止 `docker compose down -v`，它会移除持久卷。

需要恢复时先停API写入，在独立的新数据库和空数据目录完成真实恢复演练，再经过明确目标确认切换。不要直接对当前业务库运行 `pg_restore --clean`。仓库的 `scripts/backup.sh`、`scripts/restore.sh` 提供带校验清单的备份/隔离恢复路径；它们需要匹配数据库版本的PostgreSQL工具和Python依赖。容器环境未验证这些宿主机工具，不能将本机演练算成云端恢复通过。

程序回滚使用上一次已验收的API/网页镜像digest；数据库迁移前先备份，不擅自Alembic downgrade。回滚不删除数据卷。数据库如需回退，先恢复到独立新库并核对，再授权切换，不能用旧镜像能启动代替数据兼容性检查。

本地/宿主机操作示例（仅在对应数据操作得到授权后执行）：

```bash
# 按当前项目.env及终端覆盖值备份；输出必须是项目外、尚不存在的新目录。
bash scripts/backup.sh /private/tmp/shushi-backup-unique
# 也可显式指定私有的 DATABASE_URL / DATA_DIR 配置文件；不要将URL写在命令参数里。
bash scripts/backup.sh /private/tmp/shushi-backup-other --config-file /etc/shushi/backup-source.env
# 默认真正还原到工具新建的临时PostgreSQL和临时媒体目录，校验后关闭并清理临时环境。
bash scripts/restore.sh /private/tmp/shushi-backup-unique --dry-run
# 真实恢复先检查已明确配置的独立空库和空媒体目录，打印目标指纹。
bash scripts/restore.sh /private/tmp/shushi-backup-unique --inspect-target --target-config-file /etc/shushi/restore-target.env
# 核对具体目标并获授权后，再把上一步的指纹填入；不会clean或覆盖旧库。
bash scripts/restore.sh /private/tmp/shushi-backup-unique --apply --target-config-file /etc/shushi/restore-target.env --confirm-target FINGERPRINT_FROM_INSPECTION
```

`restore-target.env` 需明确写入新目标的 `DATABASE_URL` 和 `DATA_DIR`，不能复用源库或源媒体目录。真实恢复前需清除终端同名覆盖项，目标不能有业务表、公共对象或其他客户端，媒体目录不能有文件。工具会过滤归档里默认public schema的创建项，避免为恢复引入DROP；其余SQL作为一个事务应用。校验失败不会启动应用。数据库与文件系统无法做同一事务，因此恢复期间应用必须保持停止；数据库已恢复但文件切换失败时也不能直接开放产品。

备份清单只包含SHA-256、行数和来源指纹，不含URL/密码。校验和验证传输完整性，不证明备份来源可信，只允许还原自己保管的备份。数据库使用一致快照；媒体逐文件校验，不能保证与运行中数据库的跨资源原子快照，发布备份前应先经授权短暂停API写入。旧的无校验 `db.dump + manifest.txt` 备份不会自动恢复，也不会被删除。

### Compose生产维护入口

生产数据库不公开5432，因此使用维护profile访问私有网络，不要求宿主机直连 `db`。下面命令同样需要数据操作授权；本次没有执行或构建维护容器。

```bash
# 在已授权服务器构建维护镜像；后续动作均显式运行，不自动备份。
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml --profile ops build ops-backup
# 先授权短暂停止API写入，再生成一个尚不存在的备份名。
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml stop api
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml --profile ops run --rm ops-backup backup before-change-UNIQUE
# 在维护scratch卷内创建临时PostgreSQL并真正还原，校验后停止临时库。
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml --profile ops run --rm ops-backup drill before-change-UNIQUE
# 恢复原API容器，不重新构建镜像；入口仍会检查DB并执行幂等迁移后恢复任务。
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml start api
```

备份存在独立 `ops_backups` 私有卷中，不会上传云盘或公开下载；异机副本的位置和转移方式需用户批准。维护临时数据在 `ops_scratch` 卷，正常演练会清理自己创建的临时目录，异常停机后可能有残留，应核对归属后单独清理，不要删除业务卷。

独立空目标恢复使用另一个profile，先在项目外私有配置填写另一份 `RESTORE_POSTGRES_PASSWORD`，不复用源库密码：

```bash
python3 deploy/preflight.py --env-file /etc/shushi/runtime.env --restore-profile
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml --profile restore build ops-restore
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml --profile restore run --rm ops-restore inspect before-change-UNIQUE
# 核对上一步打印的独立目标指纹后，显式执行。
docker compose --env-file /etc/shushi/runtime.env -f deploy/compose.yaml --profile restore run --rm ops-restore restore before-change-UNIQUE --confirm-target FINGERPRINT_FROM_INSPECTION
```

该profile固定连接 `restore-db/shushi_restore`，不会把数据恢复到源 `db/shushi`，也不会挂载源媒体卷。已有业务表、公共对象、其他连接或媒体文件都会拒绝覆盖。此步骤不切换业务流量，完成后应用仍连接原库；业务切换需另外审查数据库/用户/卷/密码的Compose覆盖配置，并在停写后明确授权。不要通过删除卷来清空恢复目标。

## 当前验收边界

统一离线验证入口为 `bash scripts/verify.sh`，也可用 `--backend-only` 或 `--frontend-only` 分别执行。Python验证使用临时PostgreSQL，导入前关闭真实dotenv读取，清除继承的凭据与数据库配置，并禁止仓库`.env`、业务`data`读取以及Python网络连接；运维与内容处理测试仅使用隔离样例。前端执行单元测试、类型检查和Lint，不覆盖正在运行的`.next`构建目录。该入口不会触发供应商、部署、内容发布或云资源操作。本机若沙箱拒绝PostgreSQL共享内存，需允许其创建临时隔离数据库，不能改连业务库来绕过。

已准备配置与安全默认值。以下仍待实际环境验证：镜像可拉取性、Linux依赖安装、容器启动和卷权限、Caddy配置装载、DNS及证书签发、国内资源与备案条件、健康探测、私有网络/端口、冷启动/重启、备份还原、真实供应商调用与真机。没有创建云资源，没有上传密钥，也没有新增费用。

语法依据：[Docker启动健康依赖](https://docs.docker.com/compose/how-tos/startup-order/)、[Compose变量插值](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)、[Caddy反向代理](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)、[Caddy自动HTTPS](https://caddyserver.com/docs/automatic-https)。Next部分按本项目已安装版本的自托管文档准备。
