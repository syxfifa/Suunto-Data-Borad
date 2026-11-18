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

## OAuth 应用注册和两种授权方式

点击「生成授权链接」。首次由程序读取官方 OAuth 元数据，经动态客户端注册获取 client ID，注册固定回调 `http://127.0.0.1:8000/oauth/callback`。随后点击「打开 Suunto 授权页」，使用 Suunto App 的账号登录并同意。客户端使用 Authorization Code + PKCE S256。

- **手动粘贴**。通过 NAS 内网 IP 打开看板。Suunto 授权结束后，浏览器跳到 `127.0.0.1` 时可能提示无法连接。复制地址栏中包含授权码和 state 的完整回调地址，在五分钟内回到原看板页面粘贴提交。也可仅粘贴 code 值。
- **网页回调**。如需自动回调，在电脑运行 `ssh -N -L 8000:127.0.0.1:8000 用户名@服务器内网IP`，保持连接，从电脑浏览器打开 `http://127.0.0.1:8000` 发起授权。

## 验收 tools/list 和实际返回字段

在服务器的项目目录执行：

```sh
docker compose exec app python -m suunto probe
docker compose exec app python -m suunto sample list_user_routes '{"page":0,"size":1}'
```

`probe` 应返回 `status: ready` 和官方工具定义。服务端可能也列出写入工具，只有标记为 `allowed_read: true` 的工具可以由本应用调用。`sample` 的参数从真实 `inputSchema` 取得，输出字段路径和类型，省略数据值。空列表只能证明该查询没有样本，可换一个有数据的日期或对象再探测。

运行 `docker compose exec app python -m unittest discover -s tests -v` 验证授权、工具分页、令牌刷新、增量同步、网页查询、设置与导出。测试使用模拟响应和临时数据库。

## SQLite 同步验收

已运行过上一阶段的实例，继续使用原来的 Compose 目录和命名卷，授权凭据会保留。浏览器授权成功后，点击首页「立即同步」，也可以在服务器执行：

```sh
docker compose exec app python -m suunto sync
```

首次同步从近 28 天开始，恢复采样按天查询。初次可能需等几分钟。之后默认每 30 分钟运行，各数据源从自己的进度继续，并回扫前两天以更新迟到记录。路线按修改时间分页，每天额外完整扫描一次，以发现缺少修改时间的路线。已存的运动详情正常情况下只读取一次。

在首页看到活动次数、距离和时长后，再运行一次同步。记录数量应保持稳定，除非 Suunto 上有新增记录或值发生变化。失败条目可在首页展开，也可访问 `http://服务器内网IP:8000/api/status`。数据库是同一卷下的 `/data/suunto.sqlite`。升级时保留该卷。

SQLite 中的时间索引用北京时间。原始返回字段保存在 JSON 记录内。运动按 `workoutId`，睡眠按 `SleepId` 和采样时间，恢复按采样时间，日常活动按指标、来源和采样时间，路线按 `routeId` 去重。单条坏数据记入错误列表。某个日期窗口请求失败时，该源进度保留在最后成功窗口，其他数据源仍可继续。

## 使用看板

- 首页显示最近 7 天和 28 天的活动次数、距离与时长。训练趋势可按日或按周查看。活动页可填写开始和结束日期，时间范围包含结束当天。训练效果取官方详情的 PTE。
- 睡眠时长按样本所属的北京时间日期汇总。恢复曲线显示 MCP 提供的日均身体资源百分比。日常活动展示步数与能量消耗，不同数据来源各自绘制。
- 保存路线以 MCP 返回的描述作为可读标签，显示距离与运动类型。同步状态页可以查看批次和错误，也可以下载全部已入库记录的 CSV。CSV 包含数据源、去重键、北京时间索引及记录 JSON。
- 设置页可将同步间隔调整为 1–1440 分钟。浏览器所需的 Chart.js 已随镜像提供。

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
3. 推送到 `main` 后，工作流会构建 AMD64 和 ARM64 镜像，在两种架构运行测试，并检查 Compose 启动和设置持久化。验收通过后发布 `latest` 和 12 位提交哈希标签。缺少令牌时，工作流只构建和测试。

Compose 默认使用 `docker.io/jjsyx123/suunto-data-borad:latest`。需要固定版本时，可把 `image` 改成带发布标签的完整镜像名。私有镜像需先在 NAS 的终端运行 `docker login`。

## 排查

- **授权失败**。确认打开授权的浏览器与最后提交的是同一浏览器。自动回调时保持 SSH 转发；旧链接请重新生成。
- **Token 过期**。探测调用会在过期前尝试刷新。刷新凭据失效或官方未签发刷新 token 时重新授权。
- **MCP 限流**。程序按 `Retry-After` 等待，长等待由状态页提示；查看 `docker compose logs --tail 100 app`。
- **字段缺失**。重新运行 `probe` 和对应 `sample`。区分工具未开放、样本为空和单条记录缺字段，再决定页面是否显示对应指标。

[Suunto MCP 官方指南](https://mcp.suunto.cn/mcpguide) · [MCP OAuth 客户端流程](https://ts.sdk.modelcontextprotocol.io/v2/clients/oauth)
