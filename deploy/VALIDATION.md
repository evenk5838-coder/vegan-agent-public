# W11准备检查记录 · 2026-10-27

以下为本次实际运行结果，未以容器、云部署或供应商实测替代。

| 检查 | 实际结果 | 边界 |
|---|---|---|
| `python deploy/preflight.py --self-test` | 7项通过 | 合成运行配置、私有配置格式、HTTPS/Origin/cookie/单worker关系、独立恢复凭据；没有读取真实运行配置 |
| `python -m unittest discover -s scripts/tests -v` | 15项通过 | 备份/恢复部分使用新建Unix socket PostgreSQL与合成媒体；健康响应、进程列表和维护角色使用合成替身 |
| `python scripts/check_persistence.py` | 通过 | 两个独立应用/数据库进程生命周期，临时代码副本不含真实.env；4类记录及waiting_input任务保留 |
| 系统Ruby `YAML.safe_load` | 通过 | 7个service，仅Caddy发布端口；媒体源备份只读、恢复备份卷只读。不是Docker Compose原生配置验证 |
| Python编译、3个shell `bash -n` | 通过 | 静态语法 |
| `git diff --check` | 通过 | 本任务变动 |

真实隔离恢复包括：一致快照归档、归档及媒体SHA-256校验、还原后行数与合成记录值核对；拒绝错误指纹、源目标相同、非空目标、篡改文件、路径越界、符号链接与未列入清单文件。没有还原或替换真实业务库。

数据库恢复为单事务；媒体在目标文件系统内暂存，分别切换images/audio目录，可用于Docker卷挂载根。它不提供跨数据库和文件系统的原子事务，恢复全过程须保持应用停止。备份期间运行中的媒体变化也不能当作跨资源快照，正式备份须先授权停写。

本机没有Docker，本次没有运行Docker命令、构建/拉取镜像、签发证书、创建云资源、开启供应商调用或修改密钥。API/web/maintenance镜像安装与运行、Caddy原生语法验证、Compose依赖启动、Linux卷权限、生产网络与HTTPS、云上备份还原仍待实际环境验证。独立restore profile不自动切换业务流量。
