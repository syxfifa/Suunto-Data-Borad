# Suunto Data Borad 变更记录

## V1.0.3

- 镜像内置 HTTP 健康检查，精简 Compose 配置。初始化服务成功退出后启动非 root 看板。

## V1.0.2

- 默认挂载 `./suunto-storage:/data`。独立的 `storage-init` 初始化数据目录后退出，主看板始终以 UID/GID 10001 运行。
- CI 验证全新目录与已有文件的权限初始化、容器重建和隔离恢复。

## V1.0.1

- Compose 默认拉取 `latest`，容器名固定为 `suunto-data-borad`，使用 Docker 命名卷 `suunto-storage`，部署时无需调整 NAS 文件夹权限。
- CI 使用新命名卷验证启动、容器重建后保留数据和独立卷恢复。

## V1.0.0

- 单容器部署，NAS 和容器端口统一使用 7383，数据写入项目目录的 `suunto-data`。
- 支持 Suunto 授权、自动增量同步、历史补采、训练看板、路线、CSV 和 SQLite 备份恢复。
- 发布流程验证双架构、浏览器授权、全新部署、重启持久化及独立目录恢复。
