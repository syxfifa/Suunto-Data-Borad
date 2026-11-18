# Suunto Data Borad 变更记录

## 0.1.1

- 部署用 Compose 直接指定 Docker Hub 镜像；CI 使用临时覆盖文件验收刚构建的镜像。

## 0.1.0

- 建立语义化版本和 Docker Hub 版本标签。
- 提供已发布镜像的单容器 Compose 部署，内网可通过服务器 IP 和 8000 端口访问。
- 包含 Suunto OAuth、SQLite 增量同步、训练看板、同步设置和 CSV 导出。
