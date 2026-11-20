# Suunto Data Borad 使用手册

当前版本 **V1.0.2**。容器名为 `suunto-data-borad`，NAS 和容器统一使用 **7383** 端口。数据库、授权和设置保存在 Compose 文件旁的 **suunto-storage** 文件夹中。

## 第一次部署

将 [docker-compose.yml](docker-compose.yml) 粘贴到 NAS 的 Compose 项目界面并部署，或保存到已安装 Docker Compose v2 的 Linux 主机并在文件所在目录执行：

```sh
docker compose pull
docker compose up -d
docker compose ps
```

从同一内网打开 **`http://NAS内网IP:7383`**。容器显示 `healthy` 后，进入 **设置 → 连接 Suunto**，在同一浏览器完成登录和授权。确认后会自动返回看板并安排首次同步，通常从近 28 天开始。

Compose 的 `7383:7383` 左侧是 NAS 端口，右侧是容器端口。`./suunto-storage:/data` 将项目目录内的文件夹挂载到容器。Docker 在首次部署时创建缺失目录。一次性的 `storage-init` 负责初始化该目录及看板文件的权限，成功退出后，`app` 以 UID/GID `10001:10001` 启动。看到 `storage-init` 显示 `Exited (0)` 属于正常状态。通过 `docker compose exec app ls -lah /data` 可以查看文件。

更新时保留原项目目录和 `./suunto-storage`。重建容器时会继续读取该目录。已在其他路径或命名卷中保存的数据会留在原位置，安装到空目录时看板会从空库开始。此项目的 Git 和构建上下文均忽略 `suunto-storage`。

| 文件 | 内容 |
| --- | --- |
| `suunto.sqlite` | 已同步的运动、睡眠、恢复、日常活动、路线和同步进度 |
| `token.json` | Suunto 授权凭据 |
| `config.json` | 在网页保存的同步间隔、目标和地图偏好 |
| `access.json` | 启用看板密码后生成的密码哈希 |

## 看板功能

| 页面 | 怎么用 |
| --- | --- |
| **概览** | 在极光机舱选日期，看昨夜睡眠、身体资源和当天训练。页面下方有近 7/28 天、本周、上周同期和本月统计。 |
| **训练趋势** | 选择按日或周、运动类型和时间范围。点击图表柱形或展开「查看图表数据」点日期，可进入对应活动列表。 |
| **运动活动** | 输入开始和结束日期，点击「筛选」。按北京时间计算，包含结束当天。高级筛选支持名称、距离、时长和 TSS，点「查看详情」看该次运动。 |
| **运动对比** | 选择同类运动的活动 A、B。跨页时可先对一条点「应用选择」，再翻页选择另一条。 |
| **睡眠与恢复** | 查看近 7、28 或 90 天的睡眠时长、有效夜晚、身体资源及压力状态。可选健康指标在「设置 → 目标、地图与可选指标」绑定已同步到的字段。 |
| **日常活动** | 选择来源，按日、周或月看步数和千卡。设置页可指定默认来源和每日步数目标。 |
| **保存路线** | 按名称、类型、距离或更新日期筛选。点击起点、参考中心或终点查看并复制坐标。启用 OpenStreetMap 后，点击底图链接才会向该服务发送所选点的坐标。 |

运动统计中，「已确认无活动」表示该时段已查询且没有活动。「同步未完成」表示覆盖仍有缺口，页面数字是本地小计。`—` 表示暂无可用数值。

## 同步、导出与备份

- **手动同步**。打开 **设置 → 同步 → 立即同步**。自动同步默认每 30 分钟，间隔可在设置页调整。
- **补历史**。打开 **设置 → 历史补采**，选近 90 天、近一年或自定义日期。可以暂停后继续。待修复日期和失败日志也在设置中查看。
- **导出 CSV**。在 **设置 → 同步 → 自定义导出** 选择数据来源、日期和格式。运动活动页可以导出当前筛选的全部结果。分享模式会省略坐标、名称、设备来源、编号和原始 JSON。
- **维护**。在 **设置 → 维护** 查看版本、授权、SQLite 占用或下载脱敏诊断。通过可信 HTTPS 访问时，可启用看板密码。多实例分别使用不同的项目目录、容器名和 NAS 主机端口。

在 NAS 项目目录执行备份。默认包括数据库和已有设置，确需备份授权时在 `backup` 后加 `--include-token`。归档应存放在私有目录。

```sh
umask 077
archive="$HOME/suunto-backup-$(date +%F-%H%M%S).zip"
docker compose exec -T --user 10001:10001 app python -m suunto backup > "$archive"
docker compose exec -T --user 10001:10001 app python -m suunto verify < "$archive"
```

恢复前备份当前数据，确认目标 Compose 项目和数据目录。在项目目录先停止 app，用已校验的归档恢复数据库和设置。确认归档与目标数据属于同一 Suunto 账号。

```sh
docker compose stop app
docker compose run --rm --no-deps -T storage-init
docker compose run --rm --no-deps -T app python -m suunto restore --component database --replace --same-account < "$archive"
docker compose run --rm --no-deps -T app python -m suunto restore --component settings --replace < "$archive"
docker compose up -d
```

## 排查与发布

- **打不开看板**。检查 `docker compose ps`、`docker compose logs --tail 100 app`，以及 TCP `7383` 或容器名 `suunto-data-borad` 是否被其他服务占用。若日志提示 `Cannot initialize /data`，确认数据目录是 NAS 本机的可写文件夹，并支持 Linux 文件所有权。某些网络共享会禁止容器设置所有者。
- **授权失效**。从当前看板地址进入设置，重新点「连接 Suunto」。反代需通过 `FORWARDED_ALLOW_IPS` 将实际代理地址配置为可信来源。
- **限流或数据缺失**。在设置页查看连接状态、数据覆盖和重试时间。可选指标以已同步到的字段为准。
- **更新镜像**。默认使用 `jjsyx123/suunto-data-borad:latest`。执行 `docker compose pull` 和 `docker compose up -d` 后，在维护页核对版本。
- **开发验证**。容器内可执行 `docker compose exec --user 10001:10001 app python -m unittest discover -s tests -v`。GitHub Actions 在 `main` 验证双架构、浏览器授权、自动初始化、备份恢复。镜像发布使用仓库 Secret `DOCKERHUB_TOKEN`。
