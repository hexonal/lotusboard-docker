# 面板蓝绿部署

2026-11-09 已在日本面板服务器部署。运行 PHP 8.4.27，数据库没有迁移。

## 现网结构

```text
公网隧道 / Nginx Proxy Manager / 本机 v2node
                         |
v2board-new 固定 Caddy 网关（宿主机 :84，172.18.0.9 / 2001:db8::9）
                         |
         v2board-web-blue 或 v2board-web-green
                         |
       当前槽位的 worker + 唯一 scheduler
```

网关仍由 `/root/docker-compose/docker-compose.yaml` 管理，只运行 Caddy。
应用由独立项目 `lotusboard-bg` 管理，配置和脚本在
`/root/lotusboard-docker-new/bluegreen/`，源码对应本仓库 `bluegreen/`。
两个 web 均可保留运行，后台角色仅活动槽位运行。web 不发布宿主机端口。
网关管理端口仅在容器内部 `127.0.0.1:2019` 监听。

网关通过 `caddy reload` 切换 upstream，应用发布不重建网关，也不更换 :84、Docker
别名或 IP。响应头 `X-Deployment-Slot` 用于核验实际槽位。首次引入网关需要受控替换
原容器，现场探针观察到约 3.4 秒的失败采样窗口；后续切换另行验收，不能把首次接管
说成无中断。Nginx 已执行配置检查和 graceful reload。

## 发布及回退

在服务器运行以下脚本。先用 `status` 确定活动槽位，`prepare` 只能操作非活动槽位。
示例假设活动为 blue；发布到 green：

```sh
cd /root/lotusboard-docker-new/bluegreen
python3 manage.py status
python3 manage.py prepare green release-<唯一版本号> --source /root/lotusboard-build/<版本号> --image <已构建的运行镜像>
python3 manage.py promote green
python3 manage.py status
```

`--source` 指向已准备完整源码和 vendor 的独立构建目录。按应用的 `composer.lock`
执行 `composer install --no-dev --no-scripts`，不能在活动 release 上执行 Composer
或替换 vendor。使用目标 PHP 镜像构建依赖，不使用宿主机其他版本 PHP。脚本把镜像
解析为本地不可变 image ID。省略 `--source` 时会复制原生产目录，仅适合初次迁移和
复用现有已验证版本，不代表自动拉取 Git 最新提交。

`prepare` 为版本建立独立目录，等待非活动 web 的连接排空，再启动该槽位 web。
它不启动后台角色，先检查健康、Composer 平台要求和生产就绪检查。准备失败不会切换入口。
版本目录不复用、不覆盖；旧版本保留用于追溯。

`promote` 再次检查就绪后，依次停止旧 scheduler、等待当前调度结束、排空旧 worker，
验证并重载网关，启动新 worker 和 scheduler，最后从网关做完整检查。
成功后原子保存活动槽位及 Compose 重启策略。失败时自动恢复旧入口和后台角色。
交接期间网页仍由 web 提供，队列领取和调度会短暂停顿；不会同时启动两套定时任务。

回退到仍保留的 blue：

```sh
python3 manage.py promote blue
```

回退使用相同的就绪检查和后台角色交接，不回滚数据库。仅在代码、任务载荷与数据库
兼容时使用。未来涉及 schema 的发布需另行采用兼容迁移；本次没有执行迁移。

被强制中断的交接会留下 `handoff.json`，阻止直接继续发布。恢复最后确认的活动槽位：

```sh
python3 manage.py recover
```

脚本使用文件锁防止并发发布。不要直接执行两套 background profiles 的 `up`。
非活动后台容器的重启策略为 `no`，活动后台为 `always`，web 为 `always`。
Compose 的 profiles 防止普通 `up -d` 意外启动两套 scheduler。

## 共享数据与关停

源码及 vendor 按 release 只读挂载；以下数据共享原生产位置：`.env`、storage、
后台 `config/v2board.php`、`config/theme/`、`public/theme/` 和订阅 `resources/rules/`。
因此现有订阅模板更新、上传、登录文件缓存和后台设置仍然有效。
各角色分别使用 `/runtime/cache` 和 `/runtime/views`，避免编译视图跨版本混用。

`APP_DISABLE_CONFIG_CACHE=true` 使后台设置、主题及更新命令清理本槽位配置缓存，
从共享 PHP 配置读取最新设置；普通部署仍保留原 `config:cache` 行为。
共享后台配置和主题配置加入 OPcache blacklist，避免其他 FPM 池继续使用旧设置。
新主题初始化直接更新本次请求内的配置，避免等待缓存而进入无限循环。
不要手工执行 `config:cache` 或全局 `cache:clear`；后者会影响文件缓存中的登录会话。

镜像继承 PHP 的默认 `SIGQUIT`，后台包装脚本需要显式 `SIGTERM`。应用 Compose
已声明 `stop_signal: SIGTERM`，发布脚本停止后台时也显式发送 TERM。
worker 的 Supervisor 等待在途任务最多 120 秒，Docker 等待 150 秒；scheduler
先停止 cron，再等待已有 `schedule:run`。专用 crontab 显式配置 PHP 的绝对路径、PATH
以及每个角色的缓存目录环境；Debian cron 不会自动继承 Docker 的这些变量。健康检查
要求调度心跳晚于本容器启动时间且不超过 90 秒，不能只检查 cron 进程。web 停止时
PHP-FPM 使用 QUIT。
旧 web 在网关切走后保留运行，准备下个版本前等待连接排空，超时则拒绝覆盖。

不要无限延长队列任务 timeout：当前 Redis retry_after 为 90 秒，任务 timeout 应更短，
否则任务可能被重复领取。部署不修改这些队列语义。

## 验证及证据

```sh
python3 manage.py verify blue --gateway
```

当前就绪检查共 56 项，包含扩展、数据库 SELECT、隔离 Redis 键、队列访问、密码散列、
JWT、加密、Markdown、邮件 MIME、合成 Stripe 签名与回调解析、登录校验、五种订阅格式
和后端节点配置。不会发送邮件、创建真实支付或输出用户订阅 token。

`exercise.py` 会实际切到另一槽位再回退，同时探测 :84、公网隧道和公网 Nginx，
并校验切换期间慢速下载的完整性。它会改变活动槽位，应仅在受控验收中运行。
现场证据保存在服务器 `migration-evidence.json`、`exercise-evidence.json` 和
`verification-*.json`，这些文件和 state/release/runtime 均不入 Git。
实际结果见 [部署验收记录](deployment-20261109.md)。

尚未验证真实购买、邮件送达和完整交互登录。Laravel 仍为 8.83.29，框架与依赖的后续
升级不包含在这次部署中。

## 原单容器及 PHP 7.4 回退

初次接管前的 Compose 和 inspect 保存在 `bluegreen/backups/<时间>/`。
如需恢复原单容器，必须先受控停止蓝绿的后台角色，再恢复原 Compose 的面板服务并
重建；不能让原容器和蓝绿 scheduler 同时运行。原应用及 vendor 保留在旧目录。

旧 PHP 7.4 镜像和 vendor 快照继续保留，但 `rollback-php74.sh` 现在会拒绝直接操作
网关。必须先恢复原单容器拓扑，才能使用该脚本回退 PHP；普通应用回退使用 `promote`。

## 参考

- [Caddy 平滑配置重载](https://caddyserver.com/docs/getting-started#reloading-config)
- [Nginx 配置重载](https://nginx.org/en/docs/control.html)
- [Docker bind mount](https://docs.docker.com/engine/storage/bind-mounts/)
- [Laravel Horizon 优雅部署](https://laravel.com/docs/8.x/horizon#deploying-horizon)
