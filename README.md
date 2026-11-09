# lotusboard-docker

Lotusboard 的 Docker 运行环境和生产部署工具。

当前生产使用 PHP 8.4，已拆分固定网关、蓝绿 web、独立队列和单一定时任务。
部署、验证与回退步骤见 [平滑部署文档](docs/smooth-deployment.md)，工具在
[bluegreen/](bluegreen/)。运行镜像源码在 SakuraNeko 子模块的 `php84/`。

应用和运行镜像均为 Git 子模块，克隆时使用 `git clone --recurse-submodules`。
