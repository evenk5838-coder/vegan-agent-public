# Linux容器实跑

2026-10-30，独立公开副本通过[GitHub实际运行](https://github.com/evenk5838-coder/vegan-agent-public/actions/runs/41036446387)，对应源码`daaa0e777efa377d906cd66359af228d57a965c5`。

本次实际执行：

- 原Compose结构及生产Caddy配置校验。
- 从锁文件干净安装依赖，构建API、Web和维护镜像。
- 启动生产模式API与数据库，经Caddy和受信任的内部CA验证HTTPS；私有路径404、未登录库存401。
- 两个合成账号隔离，创建食材并确认库存可读。
- 重建API容器后，原登录与食材仍保留。
- 停止API写入，实际备份5张表及1个合成媒体文件，再启动临时PostgreSQL进行真正恢复、核对数据库和媒体，最后恢复源API并复查库存。

运行仅使用公开源码、合成账号、随机临时配置和本次创建的容器/卷。模型、高德、语音和真实推送均关闭且无凭据；没有上传个人数据库或媒体，没有推送镜像、上传制品或配置缓存。检查后删除本次临时栈，不操作其他项目资源。

## 再次执行

公开仓库的Actions页手动选择`Isolated Linux container smoke`，或使用已认证的GitHub CLI：

```sh
gh workflow run container-smoke.yml --repo evenk5838-coder/vegan-agent-public --ref main
```

工作流仅支持手动触发，只在公开仓库使用标准`ubuntu-24.04` runner，权限为只读源码。脚本还会核对GitHub-hosted Linux环境、固定公开仓库与事件，并拒绝含本机.env、data或供应商密钥的目录。因此不能直接在开发者电脑或生产服务器运行。fork项目需自行调整并审查仓库标识，默认不会运行。

脚本为`deploy/container_smoke.py`；针对误连生产资源、私有事件、外部数据卷、非回环端口、继承凭据及清理失败的6项离线保护测试位于`scripts/tests/test_container_smoke.py`。

## 尚未证明的事项

内部CA只验证隔离环境的TLS，不代表真实域名、DNS或公网ACME证书已准备；不能用于朋友访问。标准runner的构建成功不证明小规格腾讯云机器的容量或国内网络。尚需腾讯云实际部署、域名/HTTPS、备份与回滚检查，以及手机扫码、定位、语音、系统通知送达和统一实际对话终验。

标准公开runner的免费范围见[GitHub官方计费说明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。当前没有使用付费大规格runner、制品或缓存；该验证也不会购买腾讯云资源。
