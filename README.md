# Suunto Data Borad 使用说明

当前版本 **0.8.2**。这是用于 NAS 或 PVE 内网的 Suunto 看板。首次授权、日常同步、历史补采和 CSV 导出都可以在网页完成。

## 1. 首次部署与授权

把 [docker-compose.yml](docker-compose.yml) 放到已安装 Docker 和 Compose v2 的 NAS 或 PVE Linux 主机。在服务器终端进入该目录。**已用旧版命名卷部署的用户，请先看下面的迁移步骤。**

```sh
sudo mkdir -p ./suunto-data
sudo chown 10001:10001 ./suunto-data
sudo chmod 700 ./suunto-data
docker compose pull app
docker compose up -d
docker compose ps
```

如果服务器终端已使用 root 登录，去掉命令里的 `sudo`。在同一内网的电脑或手机打开 `http://NAS内网IP:8000`。如果已修改 Compose 主机端口，使用实际端口。

1. 打开 **设置 → 连接 Suunto**。
2. 在同一浏览器完成 Suunto 登录和授权。浏览器会自动返回看板并安排首次同步。
3. 打开 **设置 → 同步** 查看结果。首次从近 28 天开始拉取，可能需要几分钟。同步后到 **概览** 看训练统计。

### 数据保存在哪里

`./suunto-data:/data` 把 NAS 项目目录下的 `suunto-data` 文件夹挂到容器的 `/data`。升级时保留该文件夹。请用私有的 NAS 目录保存它，Git 和 Docker 构建会忽略它。

| 文件 | 内容 |
| --- | --- |
| `suunto.sqlite` | 运动、睡眠、恢复、日常活动、保存路线，以及同步进度和失败记录 |
| `token.json` | Suunto 授权凭据，授权后生成 |
| `config.json` | 同步间隔、目标、地图等网页设置，保存设置后生成 |
| `access.json` | 启用看板密码后保存的密码哈希 |

### 已部署的旧命名卷怎么迁移

如果原来的配置是 `suunto-data:/data`，在 **原 NAS Compose 项目目录**操作。先保留旧配置和旧容器，旧容器应处于运行状态。以下命令会停止旧服务，再以只读方式挂载旧卷，把完整数据复制到新目录。若项目中已有同名目录，先核对并另行保管其中的内容，避免覆盖。

```sh
(
  set -eu
  old_container="$(docker compose ps -q app)"
  test -n "$old_container"
  old_type="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Type}}{{end}}{{end}}' "$old_container")"
  test "$old_type" = volume
  old_image="$(docker inspect --format '{{.Image}}' "$old_container")"
  mkdir -m 700 ./suunto-data
  docker compose stop app
  docker run --rm --user 0:0 --volumes-from "$old_container:ro" \
    --mount "type=bind,source=$(pwd -P)/suunto-data,target=/destination" \
    "$old_image" sh -ec '
      test -f /data/suunto.sqlite
      cp -a /data/. /destination/
      chown -R 10001:10001 /destination
      chmod 700 /destination
    '
)
```

复制成功后，将新版 `docker-compose.yml` 放到原项目目录，保留部署时定制的端口等设置，然后执行：

```sh
docker compose pull app
docker compose up -d
docker compose exec app ls -lah /data
```

在网页确认原有活动和授权状态后再安排旧卷清理。如果复制失败，可以先用旧配置执行 `docker compose start app` 恢复原服务。 [Docker 数据卷迁移说明](https://docs.docker.com/engine/storage/volumes/#back-up-restore-or-migrate-data-volumes)

## 2. 各页面怎么用

| 页面 | 用法 |
| --- | --- |
| **概览** | 在极光机舱选日期，查看昨夜睡眠、晨间或日均身体资源和当天训练。下方显示近 7 天、近 28 天、本周、上周同期和本月统计。 |
| **训练趋势** | 选按日或按周、运动类型和时间范围，查看距离、时长、次数、爬升和训练负荷。点柱形图，或展开「查看图表数据」后点日期，可进入对应活动列表。 |
| **运动活动** | 输入开始、结束日期后点「筛选」。结束当天包含在结果中。「高级筛选与排序」可按名称、距离、时长和 TSS 查找。点「查看详情」可看该次活动的汇总和 Suunto 提供的累计区间数据。 |
| **运动对比** | 先选运动类型，再选活动 A、B。跨页时先选一条并点「应用选择」，然后翻页选另一条。页面也会显示该运动类型的本地最远距离和最大单次爬升。 |
| **睡眠与恢复** | 切换近 7、28 或 90 天，查看睡眠时长、有效夜晚数、身体资源和压力样本分布。HRV、睡眠阶段、静息心率等可在「设置 → 目标、地图与可选指标」绑定已经同步到的字段。 |
| **日常活动** | 选择数据来源，按日、周或月看步数和千卡。在设置中可指定默认来源和每日步数目标。 |
| **保存路线** | 可按名称、运动类型、距离和更新日期筛选。点起点、参考中心或终点，可在站内定位、缩放并复制坐标。启用 OpenStreetMap 后，点击底图链接会把所选点坐标发给该服务。 |
| **设置** | 管理 Suunto 授权、同步间隔、历史补采、显示偏好和维护。 |

所有日期按北京时间显示。运动统计中的「已确认无活动」表示该时段已查过且无活动。「同步未完成」说明已显示的数字是本地小计。`—` 表示该处暂无可用数值。

## 3. 同步、补采和导出

- **同步新活动**。点击 **设置 → 同步 → 立即同步**。默认每 30 分钟自动同步，可在设置中调整。
- **补历史数据**。打开 **设置 → 历史补采**，选择近 90 天、近一年或自定义日期。可暂停，再点「继续上次补采」。
- **检查失败记录**。在设置页查「数据覆盖」「待修复的日期窗口」和「失败日志」。到期的失败窗口会在后续增量同步时重试。
- **导出 CSV**。进入 **设置 → 同步 → 自定义导出**，选数据来源和日期。「指标列」适合用 Excel 查看，「原始 JSON CSV」保留原始字段。运动活动页可以导出当前筛选的全部匹配记录。
- **分享 CSV**。勾选「分享模式」可省略坐标、名称、设备来源、编号和原始 JSON。训练和健康指标仍属于个人数据。

## 4. 备份、恢复与升级

在 NAS 原 Compose 项目目录操作。备份默认包含数据库和已有设置。需要一并备份 Suunto 令牌时，在 `backup` 后加 `--include-token`。归档应放在私有目录。

```sh
umask 077
archive="$HOME/suunto-backup-$(date +%F-%H%M%S).zip"
docker compose exec -T app python -m suunto backup > "$archive"
docker compose exec -T app python -m suunto verify < "$archive"
```

恢复前先备份现状，再从原项目目录停止 `app`。以下示例恢复数据库和设置，归档路径使用上述 `archive` 变量。涉及已有授权时，确认归档和数据目录属于同一 Suunto 账号。

```sh
docker compose stop app
docker compose run --rm --no-deps -T app python -m suunto restore --component database --replace --same-account < "$archive"
docker compose run --rm --no-deps -T app python -m suunto restore --component settings --replace < "$archive"
docker compose up -d
```

仅当归档明确包含令牌且确认属于同一账号时，用相同方式恢复 `--component token --replace --same-account`。看板访问密码可在维护页重新设置。

日常升级前先备份，并保留 `./suunto-data`。执行 `docker compose pull app` 和 `docker compose up -d`，然后到 **设置 → 维护** 确认版本。需要锁定版本时，可在 Compose 中把镜像标签改为 `0.8.2`。

## 5. 常见问题

- **看板打不开或容器反复重启**。先运行 `docker compose ps` 和 `docker compose logs --tail 100 app`。出现 `Permission denied` 时，检查 NAS 上的项目数据目录是否允许容器 UID/GID `10001:10001` 写入。
- **授权失败或过期**。查看「连接 Suunto」旁的提示，用同一浏览器和看板地址重新发起授权。使用 HTTPS 反代时，代理应保留原 Host 与端口，向 app 转发 HTTPS 协议，并配置 `FORWARDED_ALLOW_IPS` 信任实际代理地址。
- **限流或数据缺字段**。先看「设置」中的连接状态、数据覆盖与重试时间。可选健康指标只显示 Suunto 已返回且能够使用的字段。
- **访问保护与换账号**。通过可信内网 HTTPS 反代访问时，可在 **设置 → 维护 → 访问控制** 设置看板密码。换 Suunto 账号时，使用另一项目目录和另一数据文件夹。若两套同时运行，还要使用不同的主机端口。
- **需要给别人看故障状态**。在 **设置 → 维护** 下载脱敏诊断。该页面也显示版本和 SQLite 占用。

## 6. 开发与发布

有需要时可在容器里运行 `docker compose exec app python -m unittest discover -s tests -v`。性能验收可运行 `docker compose exec -T -e PYTHONPATH=/app app python tests/benchmark_queries.py`，使用临时模拟数据。

修改部署或代码时更新 [VERSION](VERSION) 和 [变更记录](CHANGELOG.md)。GitHub Actions 在 `main` 检查双架构镜像、浏览器授权和日期筛选、旧命名卷迁移及独立目录恢复。发布使用项目 Docker Hub 账号及仓库 Secret `DOCKERHUB_TOKEN`。
