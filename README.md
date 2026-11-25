# Suunto Data Borad 使用手册

当前版本 **V1.0.21**。容器名为 `suunto-data-borad`，NAS 和容器统一使用 **7383** 端口。数据库、授权和设置保存在 Compose 文件旁的 **suunto-storage** 文件夹中。健康检查由镜像提供。

## 第一次部署

将 [docker-compose.yml](docker-compose.yml) 粘贴到 NAS 的 Compose 项目界面并部署，或保存到已安装 Docker Compose v2 的 Linux 主机并在文件所在目录执行：

```sh
docker compose pull
docker compose up -d
docker compose ps
```

从同一内网打开 **`http://NAS内网IP:7383`**。在首页点击 **连接 Suunto**，在同一浏览器完成登录和授权。授权返回后可查看首次同步进度，通常从近 28 天开始。有运动记录时会出现 **查看已同步活动**。

Compose 的 `7383:7383` 左侧是 NAS 端口，右侧是容器端口。`./suunto-storage:/data` 将项目目录内的文件夹挂载到容器。Docker 在首次部署时创建缺失目录。镜像启动时自动初始化目录和看板文件的权限，随后以 UID/GID `10001:10001` 运行看板。通过 `docker compose exec --user 10001:10001 app ls -lah /data` 可以查看文件。

更新时保留原项目目录和 `./suunto-storage`。重建容器时会继续读取该目录。从旧的双服务版升级时，替换 Compose 文件后执行 `docker compose pull` 和 `docker compose up -d --remove-orphans`，清理这个 Compose 项目遗留的 `storage-init` 容器。已在其他路径或命名卷中保存的数据会留在原位置，安装到空目录时看板会从空库开始。此项目的 Git 和构建上下文均忽略 `suunto-storage`。

| 文件 | 内容 |
| --- | --- |
| `suunto.sqlite` | 已同步的运动、睡眠、恢复、日常活动、路线和同步进度 |
| `token.json` | Suunto 授权凭据 |
| `config.json` | 在网页保存的同步间隔、目标和地图偏好 |
| `access.json` | 启用看板密码后生成的密码哈希 |
| `amap.json` | 本地高德配置和凭据。备份命令生成的归档中不包含此文件 |
| `sync-cooldown.json` | Suunto 限流时的重试期限，用于容器重启后继续等待 |
| `backup-status.json` | 最近一次通过 `verify --record` 记录的归档验证结果 |
| `restore-drill-status.json` | 最近一次通过 `drill --record` 记录的隔离恢复演练结果 |

## 看板功能

手机可从「运动活动」进入「运动对比」，桌面仍可直接进入。训练趋势、运动活动、睡眠与恢复和日常活动使用一致的日期快捷项。图表异常时可点「打开数据表」查看相同筛选下的数据，也能继续从日期进入活动。

首页可展开「数据更新」，分别查看各来源最近查询时间与最近记录日期。首页上方显示本周训练，覆盖完整时按两周同一截止时刻计算差值。「跳到日期」可直达历史复盘。活动汇总可切换本周、本月、近 7 天和近 28 天。睡眠与恢复页按睡眠、恢复和 HRV/静息心率分组，标出最近有效读数和字段状态。路线在桌面端与地图并排，手机和平板在所选路线下展开。

运动活动支持手输或用日历选择起止日期。结果上方可逐项清除条件。勾选两条同类型活动后可比较，翻页时会保留本浏览器标签页中的勾选。训练趋势可切换周报和月报，近一年默认按周聚合，仍可选按日。点「查看进阶分析」后会加载 ACWR 等指标。训练、睡眠与恢复、日常活动均可选择起止日期，或用「上一周／下一周」平移整个日期范围。

| 页面 | 怎么用 |
| --- | --- |
| **概览** | 在极光机舱选日期，看昨夜睡眠、身体资源和当天训练。页面下方有近 7/28 天、本周、上周同期和本月统计。 |
| **训练趋势** | 选择按日或周、运动类型和时间范围。点击图表柱形或展开「查看图表数据」点日期，可进入对应活动列表。 |
| **运动活动** | 输入开始和结束日期，点击「筛选」。按北京时间计算，包含结束当天。高级筛选支持名称、距离、时长和 TSS，点「查看详情」看该次运动。 |
| **运动对比** | 选择同类运动的活动 A、B。跨页时可先对一条点「应用选择」，再翻页选择另一条。 |
| **睡眠与恢复** | 查看近 7、28 或 90 天的睡眠时长、有效夜晚、身体资源及压力状态。可选健康指标在「设置 → 目标与数据偏好」绑定已同步到的字段。 |
| **日常活动** | 选择来源，按日、周或月看步数和千卡。在日常活动页可记住常用来源并编辑步数目标。周/月柱展示有效天数。 |
| **保存路线** | 按名称、类型、距离或更新日期筛选。每行显示「可预览位置」或「暂无位置点」。启用高德后自动展示当前页第一条有坐标的路线。高德关闭时，可查看示意图、复制坐标或手动在 OpenStreetMap 打开底图。 |

### 高德地图预览

**申请 Key 和安全密钥**

1. 登录[高德开放平台控制台](https://console.amap.com/)。首次使用时，按页面提示注册开发者。
2. 进入 **应用管理 → 我的应用 → 创建新应用**。应用名称可填 `Suunto Data Borad`。
3. 展开新应用，点击 **添加 Key**。Key 名称可填 `Suunto 路线地图`，服务平台选择 **Web端（JS API）**。
4. 提交后在应用的 Key 列表复制 **Key** 和对应的 **安全密钥（`jscode`）**。如果配置了域名白名单，请加入浏览器实际访问看板所用的域名。[高德官方申请步骤（含截图）](https://lbs.amap.com/api/javascript-api-v2/prerequisites)

**在看板启用**

打开 **设置 → 高德地图预览**，分别填写 Key 和安全密钥，按已知地标核对并选择 Suunto 路线坐标系，勾选「自动显示高德地图」，点击「保存地图设置」。点击「测试连接」可用公开地标验证坐标转换和底图，再打开「保存路线」核对实际标记。安全密钥保存在本地数据卷，应直接填入自己的看板。

自动预览默认关闭。开启后，打开路线页即可查看首条有坐标的路线，并会将显示位置发送给高德。同一页面切换路线会复用地图和已转换坐标。看板的高德代理每分钟最多转发 30 次请求。地图暂不可用时仍可查看和复制坐标，或稍后重试。使用说明见[高德 JS API 安全密钥文档](https://lbs.amap.com/api/javascript-api-v2/guide/abc/jscode)。

运动统计中，「已确认无活动」表示该时段已查询且没有活动。「同步未完成」表示覆盖仍有缺口，页面数字是本地小计。`—` 表示暂无可用数值。

## 同步、导出与备份

- **手动同步**。打开 **设置 → Suunto 连接与同步 → 立即同步**。留在设置页即可看到排队、处理中的来源、完成状态和变更条数。进度查询只读取本地状态。
- **自动复核**。自动同步默认每 30 分钟。在常规增量与缺口修复之后，每个数据来源每天最多复核两个已覆盖的日期，即北京时间 7 天前和 28 天前。达到 Suunto 限流时跳过复核。更早的更新可使用历史补采。
- **调度状态**。在 **设置 → Suunto 连接与同步** 查看调度器最近检查时间。临时错误会自动重试。若持续显示长时间未响应，查看 `docker compose logs --tail 100 app`。
- **补历史**。打开 **设置 → 历史补采**，选近 90 天、近一年或自定义日期。可以暂停后继续，并查看每个来源预计剩余窗口。在「同步详情与诊断」打开「待修复的日期窗口」可查看下一次自动重试时间，也可重试单个窗口。Suunto 限流期间，重启容器后仍会等待。
- **导出 CSV**。在 **设置 → 导出数据** 选择数据来源和日期。「下载分享 CSV」保留日期和统计指标，省略坐标、名称、设备来源、编号和原始 JSON。「下载原始 CSV」保留本地记录编号、完整时间和原始 JSON，适合私人存档。完整分列版便于表格分析，距离用公里、时长用分钟，`hrv_ms` 来自已绑定的恢复字段。运动活动页仍可直接导出当前筛选的全部结果。整站备份请使用下文的 ZIP 归档。
- **维护**。在 **设置 → 维护** 查看版本、授权、SQLite 占用或下载脱敏诊断。同步日志默认保留 90 天，升级时沿用已设定的天数。通过可信 HTTPS 访问时，可启用看板密码。多实例分别使用不同的项目目录、容器名和 NAS 主机端口。

在 NAS 项目目录执行备份。默认包括数据库和已有设置，确需备份授权时在 `backup` 后加 `--include-token`。归档应存放在私有目录。

```sh
umask 077
archive="$HOME/suunto-backup-$(date +%F-%H%M%S).zip"
docker compose exec -T --user 10001:10001 app python -m suunto backup > "$archive"
docker compose exec -T --user 10001:10001 app python -m suunto verify --record < "$archive"
```

恢复前备份当前数据，确认目标 Compose 项目和数据目录。在项目目录先停止 app，用已校验的归档恢复数据库和设置。确认归档与目标数据属于同一 Suunto 账号。

```sh
docker compose stop app
docker compose run --rm --no-deps -T app python -m suunto restore --component database --replace --same-account < "$archive"
docker compose run --rm --no-deps -T app python -m suunto restore --component settings --replace < "$archive"
docker compose up -d
```

`verify --record` 成功后，维护页会显示本次校验时间、归档日期和归档版本。恢复到空目录后，在设置中连接原 Suunto 账号并重新配置高德地图。默认归档不含这些凭据。

### NAS 定时备份和隔离恢复演练

在 NAS 任务计划里添加每日脚本，替换下方两个目录。任务账户需要能执行 Docker Compose。使用仅你可读写的备份目录，最好放在另一存储位置。

```sh
set -eu
cd '/path/to/suunto-compose-project'
backup_dir='/path/to/private-backups'
umask 077
mkdir -p "$backup_dir"
tmp="$(mktemp "$backup_dir/.suunto-XXXXXX")"
trap 'rm -f "$tmp"' 0
docker compose exec -T --user 10001:10001 app python -m suunto backup > "$tmp"
docker compose exec -T --user 10001:10001 app python -m suunto verify --record < "$tmp"
mv "$tmp" "$backup_dir/suunto-backup-$(date +%Y%m%d-%H%M%S).zip"
```

升级前或定期挑一份归档做演练。命令会在容器临时目录恢复数据库和已有设置，然后检查恢复后的 SQLite。生产卷只写入演练结果摘要，无需停止看板。

```sh
docker compose exec -T --user 10001:10001 app python -m suunto drill --record < "$archive"
```

在 **设置 → 维护 → 备份与恢复** 查看归档验证和隔离恢复演练结果，也可展开「查看 NAS 操作命令」。演练只恢复数据库和设置。

### 固定版本与回滚

想固定版本，可将 Compose 的 `image` 改为 `jjsyx123/suunto-data-borad:1.0.21`，执行 `docker compose pull` 和 `docker compose up -d`。从 V1.0.15 或更早版本升级时会自动更新 SQLite 结构。更新前保存并验证备份，回滚到 V1.0.15 或更早版本时，先用旧版镜像恢复升级前的数据库，然后启动旧版应用。

GitHub Actions 的 **Scan published Suunto image** 每周一北京时间约 09:17 扫描 Docker Hub 当前发布的双架构 `latest`，也可以手动运行。报告按镜像摘要保存，发现已有修复的高危或严重项时标记此次扫描失败。

## 排查与发布

- **打不开看板**。检查 `docker compose ps`、`docker compose logs --tail 100 app`，以及 TCP `7383` 或容器名 `suunto-data-borad` 是否被其他服务占用。若日志提示 `Cannot initialize /data`，确认数据目录是 NAS 本机的可写文件夹，并支持 Linux 文件所有权。某些网络共享会禁止容器设置所有者。
- **授权失效**。从当前看板地址进入设置，重新点「连接 Suunto」。反代需通过 `FORWARDED_ALLOW_IPS` 将实际代理地址配置为可信来源。
- **限流或数据缺失**。在设置页查看连接状态、数据覆盖和重试时间。可选指标以已同步到的字段为准。
- **访问保护**。在维护页查看密码是否启用。共享网络或通过域名开放时，通过可信 HTTPS 启用看板密码，避免他人访问健康数据和导出接口。
- **更新镜像**。默认使用 `jjsyx123/suunto-data-borad:latest`。执行 `docker compose pull` 和 `docker compose up -d` 后，在维护页核对版本。
- **开发验证**。容器内可执行 `docker compose exec --user 10001:10001 app python -m unittest discover -s tests -v`。可执行 `docker compose exec --user 10001:10001 app python -m tests.benchmark_queries`，在容器的临时目录用合成数据测量应用启动、SQLite 并行读写和批量导出。
- **发布追溯**。面向 `main` 的拉取请求会运行测试、升级与恢复验证及双架构漏洞扫描。只有 `main` 的发布流程会使用 `DOCKERHUB_TOKEN` 推送镜像。扫描执行失败会阻止发布，有修复版本的高危或严重系统包及 Python 依赖漏洞也会阻止发布。尚无修复版本的发现记录在报告中。成功发布后，版本、提交短 SHA 和 `latest` 必须指向同一镜像摘要。可在 GitHub Actions 的 `suunto-release-report` 下载双架构扫描结果和版本、完整提交 SHA、镜像摘要。
