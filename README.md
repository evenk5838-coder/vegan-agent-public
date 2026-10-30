# 蔬食 Personal Agent

一个面向蔬食日常、以对话为主的个人助手。用户可以拍配料表、找素食餐厅、搜索菜谱，以及记录和管理冰箱里的食材；四个快捷入口与聊天使用相同的服务和账号数据。

这是开发中的本地内测项目。配料识别、餐厅列表与页内地图、20道中文菜谱的搜索与做法展示、食材登记与保守食用安排，已有用户分项体验反馈；当前电脑的按住说话、自动发送及“素食”词语实录样例也已确认。上述反馈不代表所有设备、所有食品或所有餐厅均经过验证。手机扫码、HTTPS环境的录音和定位、系统通知送达及云端部署仍待验收。

## 架构

- 前端：Next.js、React、TypeScript，手机优先，消息区独立滚动。
- 后端：FastAPI、SQLAlchemy、PostgreSQL；一个 Personal Agent 调用四类业务工具。
- 外部服务：DeepSeek负责模型对话，高德提供餐厅检索和地图，AI HubMix提供中文语音转写。相关凭据由使用者自行配置。
- 数据：账号隔离的聊天、食材、个人菜谱与私有媒体。公开代码不含开发者账号、聊天、定位、录音、API Key或业务数据库。

详见[架构说明](docs/ARCHITECTURE.md)和[当前状态](docs/STATUS.md)。

## 本地运行

以下步骤适用于当前使用的macOS开发方式，需要Python 3.12、Node.js 22和npm。其他系统的本地PostgreSQL启动方式尚未验证；容器部署文件属于待实测的准备包。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock.txt
npm --prefix frontend ci
cp .env.example .env
.venv/bin/python scripts/dev.py
```

打开 <http://127.0.0.1:3000/>，创建自己的账号。后端监听8000，前端监听3000。不要在已有预览进程运行时重复启动。

`.env`模板默认关闭外部调用；未配置Key时，相应外部功能会明确不可用。请在本机填写需要的凭据并自行确认计费后再开启。`DATABASE_URL`留空时，开发入口会使用项目`data/postgres`中的本地数据库；数据库、上传媒体及`.env`均不应提交到Git。新数据库不会自动复制开发者原有的20道菜谱或个人记录。可选草稿示例在[samples/howtocook-20.json](samples/howtocook-20.json)，可先离线预览结构与标签：

```sh
.venv/bin/python scripts/import_knowledge.py samples/howtocook-20.json --dry-run --recompute
```

实际导入默认草稿，不自动发布。回归夹具与示例范围见[说明](docs/FIXTURES.md)。

语音采用打字/语音切换、按住说话、松开自动转写并提交给Agent、上滑取消。浏览器和系统的麦克风权限、所选输入设备都会影响实际效果。

## 离线验证

安装依赖后执行：

```sh
bash scripts/verify.sh --backend-only
bash scripts/verify.sh --frontend-only
```

后端测试使用临时PostgreSQL，禁止读取真实配置和业务数据并禁用互联网；前端入口执行单元测试、类型检查和Lint。它们不会调用真实模型、地图、转写或推送服务，也不代表手机和云端验收通过。

生产部署准备见[deploy/README.md](deploy/README.md)。请自行设置域名、HTTPS、私有运行配置和持久化数据库，并独立验证备份恢复。

## 内容与许可

代码许可见[LICENSE](LICENSE)。菜谱和外部资料的许可与代码许可分开，见[素材及第三方内容说明](ASSETS.md)。素食标签由配料规则计算，未知复合配料保留待核对；食材食用安排是有条件的日常提醒，不能代替包装标注、实际储存判断或食品安全专业意见。
