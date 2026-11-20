# Suunto 内网看板

单个 FastAPI 容器，固定访问官方中国区 Suunto MCP。OAuth、启动探测、SQLite 增量同步、Jinja2 看板、网页设置和 CSV 导出均已实现。

## 开发顺序

1. **授权与探测**。有 token 时启动即进行 MCP 握手和分页 `tools/list`。首次启动等待授权，授权完成后立即探测。输出服务端声明的输入／输出 Schema。对允许的只读工具，另用样本确认实际字段路径与类型。
2. **SQLite 与同步**。SQLite 保存各数据源的记录、游标、同步批次和失败条目。每次运行确认对应工具已由服务端公开，按数据源独立推进游标。
3. **网页、设置和导出**。Jinja2 和本地静态 Chart.js 提供训练趋势、活动筛选、睡眠与恢复、日常活动、路线和同步状态。页面缺失值显示为空。默认轮询 30 分钟，设置页保存后即时重排下一次任务。时区固定为 Asia/Shanghai。
4. **NAS/PVE 验收与可选 CI**。Compose 报告容器健康状态。GitHub Actions 构建并测试 AMD64、ARM64 镜像，配置 Docker Hub 凭据后自动发布。

## NAS / PVE 首次启动

把 `docker-compose.yml` 上传到装有 Docker 和 Compose v2 的 NAS 或 PVE 中的 Linux 虚拟机。升级时用它覆盖原目录里的同名文件，以继续使用已有数据卷。在服务器 SSH 终端进入该目录后执行：

```sh
docker compose pull app
docker compose up -d
docker compose ps
```

在同一内网的电脑或手机上打开 `http://服务器内网IP:8000`。Compose 将 8000 端口发布到服务器的所有网络接口。此看板供内网使用，请勿通过路由器端口转发暴露到公网。容器卷保存 `/data/token.json`、`/data/suunto.sqlite` 和设置页保存产生的 `/data/config.json`。

## Suunto 授权

用 NAS 内网 IP 或你部署时使用的域名和端口打开看板，进入「设置」，点击「连接 Suunto」，在同一浏览器登录并确认授权。程序会向 Suunto 注册以当前看板地址为基础的回调，使用 Authorization Code + PKCE S256。十分钟内完成授权后，浏览器会自动返回设置页，显示授权结果，并安排一次同步。使用反向代理时，保留浏览器访问时的 `Host`（含端口）；如果代理终止 HTTPS，向应用转发协议，并在 Uvicorn 中信任该代理的地址。

每位部署者在自己的实例中完成授权。访问令牌、刷新令牌和同步后的个人数据保存在实例的数据卷中。Git 和 Docker 构建上下文会忽略部署产生的令牌文件、设置文件和 SQLite 数据库。

## 验收 tools/list 和实际返回字段

在服务器的项目目录执行：

```sh
docker compose exec app python -m suunto probe
docker compose exec app python -m suunto sample list_user_routes '{"page":0,"size":1}'
```

`probe` 应返回 `status: ready` 和官方工具定义。服务端可能也列出写入工具，只有标记为 `allowed_read: true` 的工具可以由本应用调用。`sample` 的参数从真实 `inputSchema` 取得，输出字段路径和类型，省略数据值。空列表只能证明该查询没有样本，可换一个有数据的日期或对象再探测。

运行 `docker compose exec app python -m unittest discover -s tests -v` 验证授权、工具分页、令牌刷新、增量同步、网页查询、设置与导出。测试使用模拟响应和临时数据库。

## SQLite 同步验收

已运行过上一阶段的实例，继续使用原来的 Compose 目录和命名卷，授权凭据会保留。浏览器授权成功后，可在设置页点击「立即同步」，也可以在服务器执行：

```sh
docker compose exec app python -m suunto sync
```

首次同步从近 28 天开始，恢复采样按天查询。初次可能需等几分钟。之后默认每 30 分钟运行，各数据源从自己的进度继续，并回扫前两天以更新迟到记录。路线按修改时间分页，每天额外完整扫描一次，以发现缺少修改时间的路线。已存的运动详情正常情况下只读取一次。

设置页的「历史补采」支持近 90 天、近一年和自定义日期。它分段读取运动、睡眠、恢复和日常活动，并在每个窗口写入数据时保存进度。定时同步或点击「立即同步」时，当前补采窗口结束后优先执行增量，随后继续补采。重启后继续已排队的任务，手动暂停的补采需点击「继续上次补采」。替换补采范围只重置任务进度，已入库数据保留。

设置中的「待修复的日期窗口」会分别显示每段失败范围、失败次数和可重试时间。每批增量先处理新数据，再尝试最多四个到期缺口。初始等待 15 分钟，连续自动修复失败时增加等待时间。选择旧日期筛选运动时，也可看到相应范围的同步故障提示。补采因坏记录停在某一窗口时，可在窗口自动修复后继续补采剩余范围。

从旧版本升级时，已有记录和授权保留。历史查询覆盖从升级后成功处理的窗口开始记录，可补采近 90 天来确认训练趋势的整个显示范围。单个窗口内遇到损坏记录，历史补采会保留在待重试窗口，并继续处理其他数据源。路线连续两次成功全量扫描均未出现才从网页列表隐藏，原始记录仍可在 CSV 中查看。再次收到该路线时会恢复显示。

在首页看到活动次数、距离和时长后，再运行一次同步。记录数量应保持稳定，除非 Suunto 上有新增记录或值发生变化。失败条目可在设置页查看，也可访问 `http://服务器内网IP:8000/api/status`。数据库是同一卷下的 `/data/suunto.sqlite`。升级时保留该卷。

SQLite 中的时间索引用北京时间。原始返回字段保存在 JSON 记录内。运动按 `workoutId`，睡眠按 `SleepId` 和采样时间，恢复按采样时间，日常活动按指标、来源和采样时间，路线按 `routeId` 去重。单条坏数据记入错误列表。某个日期窗口请求失败时，该源进度保留在最后成功窗口，其他数据源仍可继续。

## 使用看板

- 「极光机舱」首页可选择北京时间日期，查看睡眠、晨间或日均身体资源、当日活动和周度图表。近 7/28 天及周月概览保留在首页下方。

- 首页提供近 7/28 天、本周、上周同期和本月统计，以及可选每周距离目标。运动列表支持按项目、名称、距离、时长和 TSS 筛选与排序；详情页展示官方提供的运动指标和累计区间时长，可选两条同类活动对比，并查看本地最佳。
- 训练趋势支持按日/周和运动类型查看距离、时长、次数、爬升、TSS、TSS 覆盖及平均 PTE。点击柱形或展开「查看图表数据」后点击日期，可打开对应活动列表并返回原视图。首页及周月报只有确认过的空区间才显示零；同步未完成时标明已入库小计，有活动缺少字段时对应总量用「—」显示。ACWR 使用近 7 天 TSS 除以近 28 天 TSS 的周均值，仅在有完整查询覆盖、每条活动有 TSS 且分母大于零时计算。
- 睡眠页尽可能按醒来日归档，分别显示有有效睡眠时长的夜晚数、原始样本数和最近有效日期。日期不明时，同日同 SleepId 保留最长的样本。恢复页面说明晨间首读和日均的取值。可选指标须在「设置 → 目标、地图与可选指标」选择本地已采到的字段，配置后可查看其路径、单位和近 90 天有效样本比例。
- 日常活动可按来源单独查看日、周、月步数和千卡，在设置中选择默认来源与每日步数目标。缺少数据的日期留空。
- 保存路线支持按名称、运动类型、距离、更新日期、手表状态和可见性筛选。可在本地预览位置、缩放、拖动和复制坐标；官方详情工具可用时能按需补齐缺失的点。在设置中启用 OpenStreetMap 后可打开独立底图，点击链接才会把所选点坐标发送给 OpenStreetMap。CSV 包含数据源、去重键、北京时间索引及记录 JSON。
- 设置页可将同步间隔调整为 1–1440 分钟。浏览器所需的 Chart.js 已随镜像提供。

## 导出和日常维护

- **设置 → 同步 → 自定义导出**支持来源、起止日期、原始 JSON CSV 或带单位的指标列。运动活动页也可导出当前筛选下所有页的结果。日期按北京时间，包含结束当天。
- **分享模式**只导出指标和日期，省略坐标、名称、设备来源、记录编号及原始 JSON。运动和健康指标仍属于个人数据，分享前请检查内容。
- **设置 → 维护**显示版本、SQLite 占用、记录数、授权状态、最近同步时间，可下载脱敏诊断。诊断仅含运行汇总，不含日志正文、令牌、个人域名或原始记录。日志默认保留 90 天，可设为 7–365 天，最近一次成功同步仍可查看。
- `/healthz` 用于 Compose 的存活检查。`/readyz` 返回最近已知的 MCP 探测和授权状态，未授权或无法确认可用时返回 `503`。访问控制启用时，`/readyz` 和数据接口均需要看板密码。
- SQLite 保留原始 JSON。活动对比每页读取 50 条候选，已选活动可跨页保留。保存路线在 SQLite 中筛选和计数，每页读取 25 条记录的完整内容。可用一次性模拟卷测一年和三年数据的页面耗时、SQLite 批量写入和文件体积。测试只写入临时目录，使用模拟数据。

```sh
docker compose exec -T -e PYTHONPATH=/app app python tests/benchmark_queries.py
```

### 指标字段与单位

| 看板指标 | MCP 记录字段 | 看板口径 |
|---|---|---|
| 活动距离与时长 | `totalDistance`、`totalTimeSeconds` | 米转公里，秒转分钟或小时 |
| 爬升与训练效果 | `summary.ascent`、`summary.pte` | 米和 PTE 原值 |
| 心率与训练负荷 | `hrAvgValue`、`tss` | bpm 和 TSS 原值 |
| 睡眠与恢复 | `entryData.Duration`、`entryData.Balance` | 秒转小时，0–1 的身体资源值转百分比 |
| 自选健康指标 | 部署者绑定的 `entryData` 路径 | 按设置确认的单位和字段类型展示 |

活动的通用字段映射集中在 `suunto/views.py` 的 `WORKOUT_FIELDS`。字段缺失或类型变化时保留原始记录，数值统计显示缺失。

## NAS 备份、恢复和回滚

在 NAS 的 Compose 目录运行以下命令。在线备份通过 SQLite Backup API 包含已提交的 WAL 数据，可以在服务运行中执行。归档应保存到私有备份目录。

```sh
umask 077
archive="$HOME/suunto-backup-$(date +%F-%H%M%S).zip"
docker compose exec -T app python -m suunto backup > "$archive"
docker compose exec -T app python -m suunto verify < "$archive"
```

归档默认包含数据库和已存在的 `config.json`。仅使用 `backup --include-token` 才会备份 OAuth 凭据。ZIP 本身没有加密。看板密码哈希保存在独立的 `access.json`，不会打包。归档、个人配置和导出文件应留在私有目录。

自 `0.8.1` 起，备份清单包含应用版本、SQLite `user_version`、UTC 创建时间及组件大小和 SHA-256。校验与恢复都会检查组件完整性，也接受升级前生成的旧 ZIP。数据库在新版首次启动时从未标记版本迁移到结构版本 1。恢复新版归档请用支持该清单的镜像。

从 `0.3.x` 首次升级时，旧容器没有备份命令。定义上面的 `archive` 变量后，先拉新镜像，用同一 Compose 项目下的新镜像读取原卷。验证成功后再重建服务。

```sh
docker compose pull app
docker compose run --rm --no-deps -T app python -m suunto backup > "$archive"
docker compose run --rm --no-deps -T app python -m suunto verify < "$archive"
docker compose up -d
```

恢复会替换选中的组件。先备份当前状态，再验证目标归档，确认原 Compose 项目对应正确的数据卷。

```sh
docker compose stop app
docker compose run --rm --no-deps -T app python -m suunto restore --component database --replace --same-account < "$archive"
docker compose run --rm --no-deps -T app python -m suunto restore --component settings --replace < "$archive"
docker compose up -d
```

已有组件需要 `--replace`。数据卷已有 OAuth 凭据时，恢复数据库需要确认 `--same-account`。带令牌的归档只有在明确使用 `--component token --same-account` 时才恢复令牌。账号归属不明时使用独立数据卷。恢复检查结构和完整性，并要求 app 停止。

回滚镜像时，在原 Compose 文件把 `image` 固定为上一版标签（例如 `jjsyx123/suunto-data-borad:0.3.1`），再拉取并重建。保留数据卷。需要恢复旧数据时，在固定旧标签之前用新版镜像运行上述维护命令。回滚到没有内置访问控制的版本前，由 HTTPS 代理或 NAS 网络策略保护访问。

## 内网访问控制与账号隔离

使用 NAS 现有的 HTTPS 反向代理。保留原 `Host`（含端口），由代理重写 `X-Forwarded-Proto`，并在 Compose 的 `app.environment` 中为 Uvicorn 设置 `FORWARDED_ALLOW_IPS` 为实际可信代理地址或专用子网。用 NAS 防火墙或独立网络限制后端端口的访问范围。通过 HTTPS 打开 **设置 → 维护 → 访问控制**，为用户名 `suunto` 设置独立看板密码。功能默认关闭，仍可按原方式使用内网 HTTP。启用后，容器内 probe/sync CLI 可加全局选项 `--authenticate` 交互输入密码。

- **内网 IP 直连**。在 NAS 防火墙或上游防火墙允许自己的 LAN/VLAN 访问 Compose 主机端口（默认 TCP 8000），拒绝其他来源。从允许的设备打开 `http://NAS内网IP:8000` 验证。不要在路由器配置端口转发。
- **内网 HTTPS 反代**。用部署者自选的域名和 HTTPS 端口访问。反代与 app 同机时，可把 Compose 的主机端端口绑定在回环接口，由反代访问后端。反代独立部署时，将后端端口限制到反代来源。从浏览器完成一次 Suunto 授权，确认返回同一域名和端口。防火墙配置后用不在允许范围的设备验证后端端口不可达。

忘记看板密码时，在 NAS 上用 `docker compose stop app` 停止服务，运行 `docker compose run --rm --no-deps -T app python -m suunto access-reset`，再运行 `docker compose up -d` 启动并重新设置。看板密码独立于 Suunto 账号。

在 **设置 → 维护 → Suunto 账号** 可以清除本地令牌。已有记录仍保存在卷中。此操作仅断开当前实例，不调用 Suunto 撤销接口。由于 MCP 未提供可供本应用可靠比对的稳定账号标识，旧卷重连须确认是原账号。新账号应从独立的 Compose 项目启动：

```sh
docker compose stop app
docker compose -p suunto-second up -d
```

要同时运行两套，在第二份 Compose 文件使用不同主机端口。新实例首次进入设置页后自行授权，原项目和卷可以之后重新启动。

## NAS/PVE 升级验收

在原项目目录运行以下命令。保留原有 Compose 项目和命名卷，可继续使用已有授权、数据库和配置。

```sh
docker compose config --quiet
docker compose pull app
docker compose up -d
docker compose ps
docker compose exec app python -m unittest discover -s tests -v
```

1. 等到 `docker compose ps` 中的 `app` 显示 `healthy`，或打开 `http://服务器内网IP:8000/healthz`，确认返回 `{"status":"ok"}`。这表示网页服务和 SQLite 可以访问。账号授权状态由下一步单独检查。
2. 执行 `docker compose exec app python -m suunto probe`。若为 `authorization_required`，先在网页授权。显示 `ready` 后执行 `docker compose exec app python -m suunto sync`。在看板的「同步状态」页确认写入数量和失败原因。连续两次同步时，若远端记录无新增或修订，第二次写入应为 0。
3. 在看板的「设置」页保存一个同步间隔，运行 `docker compose restart app`。待容器恢复健康后，确认间隔和已有数据仍在。

## 可选 Docker Hub 发布

1. 把完整项目（包括隐藏的 `.github/` 目录）提交到 GitHub，在 Docker Hub 创建 `suunto-data-borad` 仓库。
2. 工作流的 Docker Hub 用户名为 `jjsyx123`。在 GitHub 仓库的 Settings → Secrets and variables → Actions 添加具有镜像写权限的 `DOCKERHUB_TOKEN`。
3. 推送到 `main` 后，工作流会构建 AMD64 和 ARM64 镜像，在两种架构运行测试。Chromium 用模拟服务验证 IP、不同域名与端口、HTTPS 反代授权和日期筛选。Compose 使用 `0.7.0` 镜像生成旧数据卷，再验证原地升级和全新卷恢复。验收通过后发布 `latest`、12 位提交哈希和版本标签。缺少令牌时，工作流只构建和测试。

现有 Compose 使用 `jjsyx123/suunto-data-borad:latest`，手动拉取会跟随新发布。希望固定部署版本时，在 NAS 的 Compose 文件中使用 `image: "jjsyx123/suunto-data-borad:0.8.1"`。需要升级时改成下一版本标签再拉取，回退时改回已验证的标签。私有镜像需先在 NAS 的终端运行 `docker login`。

## 版本管理

修改应用、测试、镜像或部署配置时，同时更新 `VERSION` 和 [变更记录](CHANGELOG.md)。修复升补丁版本，新增兼容功能升次版本，破坏兼容性的调整升主版本。提交前运行 `python -m unittest discover -s tests -v`，把代码、版本和变更记录一起提交到 GitHub。发布通过后，NAS 可拉取 `latest` 升级，或者固定到指定版本。

## 排查

- **授权失败**。查看「连接 Suunto」按钮旁的提示。使用同一个看板地址和浏览器发起并完成登录。旧链接或会话过期时，回到设置页重新连接。
- **Token 过期**。探测调用会在过期前尝试刷新。刷新凭据失效或官方未签发刷新 token 时重新授权。
- **MCP 限流**。程序按 `Retry-After` 等待，长等待由状态页提示；查看 `docker compose logs --tail 100 app`。
- **字段缺失**。重新运行 `probe` 和对应 `sample`。区分工具未开放、样本为空和单条记录缺字段，再决定页面是否显示对应指标。

[Suunto MCP 官方指南](https://mcp.suunto.cn/mcpguide) · [MCP OAuth 客户端流程](https://ts.sdk.modelcontextprotocol.io/v2/clients/oauth)
