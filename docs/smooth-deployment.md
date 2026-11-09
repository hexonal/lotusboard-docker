# 面板平滑部署方案

这是一份基于现网核验的部署设计，尚未把生产入口改造成蓝绿部署。
当前 PHP 8.4 容器和路由继续按已验证配置运行。

## 现网及限制

活动项目为 `/root/docker-compose/docker-compose.yaml`，服务和容器均为
`v2board-new`。该容器同时运行 Caddy、PHP-FPM、Horizon、4 个邮件队列进程和 cron。
宿主机 `84:80` 直接发布这个容器；应用、vendor、日志和定时任务使用宿主机 bind mount。

现场确认了两种入口：

- 宿主机 `127.0.0.1:84`：本机 v2node 使用它读取面板配置，host 网络下的隧道也可能使用宿主机入口。
- Docker 网络 `v2board-new:80`：Nginx Proxy Manager 的 `v2.996icu.wiki` 站点引用此地址。

因此，仅给公网 Nginx 更换 upstream 不足以覆盖全部入口。仅增加停机等待时间，或
`docker compose up --force-recreate`，也不会让同一个 84 端口的容器替换变成无中断。

## 推荐结构

```text
公网 Nginx / 隧道 / 本机 v2node
                  |
     固定网关（宿主机 :84，Docker 别名 v2board-new:80）
                  |
        blue-web 或 green-web
        每个版本有独立源码及 vendor

独立 worker + 唯一 scheduler → 共用现有 Redis 和数据库
```

固定网关与应用分开维护；后续发布应用版本时，不替换网关容器。
网关可以使用独立 Caddy 或 Nginx。使用 Caddy 时需要允许容器内部的本地管理接口，
然后通过 `caddy reload` 切换 upstream；现有应用 Caddy 配置有 `admin off`，不能
直接把它当作支持 reload 的网关。管理端口只在网关内部监听，不发布到宿主机。

网关必须同时继承宿主机 84 端口和 Docker 别名 `v2board-new`。
当前应用正占用它们，因此第一次引入网关需要一次受控切换，不能宣称这一步已经零中断。
公网 Host、原始协议和可信代理链需要验证，以免产生错误的回调地址或重定向。

## 发布过程

1. 为新提交建立独立 release 目录，构建完整的源码和 vendor。
   使用仓库内的 `composer.lock` 执行 `composer install --no-dev`，不要在活动
   `/www/vendor` 上运行 `composer update` 或直接覆盖 vendor。
   默认 Composer scripts 可能加载应用；构建应使用隔离环境，或先 `--no-scripts`，
   再在新 release 中有控制地执行 package discovery。
2. 启动新 web 槽位，先设置 `RUN_WORKERS=false`、`RUN_SCHEDULER=false`。
   新旧 web 使用不同的 release 和容器名，不争抢 84 端口。数据库和 Redis 沿用现网。
3. 在新版本上检查扩展、Composer 平台要求、应用启动、数据库/Redis、订阅格式、
   登录校验、Stripe 签名和节点 API。PHP-FPM ping 只证明进程存活，不能代替这些就绪检查。
4. 验证新 upstream 配置后，平滑 reload 固定网关，使新请求进入新 web。
   保留旧 web，等待其在途请求完成，不立即停止旧容器。
5. 交接后台角色：停止旧 scheduler，等待当前 schedule invocation 完成；
   对旧 worker 的 Supervisor 程序执行受控停止，等已领取的任务完成，再启动新 worker。
   使用独立的 worker/scheduler 服务配置；不要只在容器内临时改开关，否则重启后角色会漂移。
6. 核验新 web、worker、scheduler、节点拉取、队列积压和公网响应，保留旧 release
   及对应运行镜像用于回退。旧 web 完成请求排空后才可以停止。

当前 Supervisor 程序名为 `horizon`、`v2board:v2board_00` 至 `_03`、`cron`。
可用 `supervisorctl -c /etc/supervisor/lotusboard.conf` 精确控制当前容器内的角色。
`horizon:terminate` 会让当前任务完成后退出，但当前 Supervisor 会自动拉起它；
交接版本时不能依靠它来永久停掉旧 worker，也不要同时广播给新旧 Horizon。

## 等待时间与共享状态

- worker 当前设置了 `stopwaitsecs=120`；Compose 的 `stop_grace_period` 应配置为
  至少 150 秒，避免 Docker 默认的 10 秒早于 Supervisor 的排空时间。
- PHP-FPM 需要优雅停止时使用 QUIT，并配置足够的等待时间；当前镜像没有把容器
  停止等同于 HTTP 请求排空。旧 web 应在网关已切走、请求排空之后才停止。
- 队列任务 timeout 必须短于 Redis `retry_after`；原队列当前是 90 秒，不能为了
  部署把任务无限延长，造成任务在旧 worker 尚未结束时被再次领取。
- 用户上传文件、现有文件缓存及认证会话需要保留，Redis 也继续使用原实例。
  当前 AuthService 的登录会话依赖缓存，部署时不要直接 `cache:clear`。
- Blade 编译目录和 `bootstrap/cache` 应按 release 隔离。可以为各 web 槽位指定
  独立 `VIEW_COMPILED_PATH`，避免新旧代码共用同一路径的编译视图。
- Docker bind mount 按发布目录创建。不能假设更换宿主机上的 `current` 软链接，
  已运行容器的 `/www` 就会自动切换到新目录；每个槽位应绑定明确的 release 路径。
- 数据库迁移另行采用向前、向后兼容的扩展/收缩过程。回滚 web 不会回滚数据库；
  此次 PHP 升级没有执行数据库迁移。

## 回滚

在 schema 和任务载荷兼容的前提下，把网关 upstream reload 回旧 web，
待新 worker 的在途任务完成后，再交还 worker 和 scheduler 所有权。
不要在旧进程还运行时，把其 vendor 替换为新版本。

已经部署的 PHP 7.4 回退仍使用 `SakuraNeko/php84/rollback-php74.sh`，那是一次
容器及 vendor 回退，存在切换窗口，不能冒充上述蓝绿 web 回滚。

## 参考

- [Nginx 配置重载与旧请求处理](https://nginx.org/en/docs/control.html)
- [Caddy 平滑配置重载](https://caddyserver.com/docs/getting-started#reloading-config)
- [Docker bind mount 行为](https://docs.docker.com/engine/storage/bind-mounts/)
- [Laravel Horizon 部署和优雅退出](https://laravel.com/docs/8.x/horizon#deploying-horizon)
