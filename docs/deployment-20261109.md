# 2026-11-09 蓝绿部署验收记录

服务器日期为北京时间 2026-11-09。当前活动槽位为 **blue**，PHP **8.4.27**。

## 已部署

- `v2board-new` 改为固定 Caddy 网关，保留宿主机 84 端口、Docker 别名以及原 IPv4/IPv6。
- web blue/green 使用独立只读源码、vendor 和编译缓存，共享原后台设置、主题、订阅模板、storage 与登录缓存。
- 活动槽位独占 Horizon、4 个邮件 worker 和 scheduler；非活动后台已停止，退出码 0、restart=no。
- 两个 web、网关、活动后台与 v2node 均 healthy，检查时 restart_count=0。
- 没有数据库迁移，没有重启主机或其他业务容器；Nginx 仅做配置检查和 graceful reload。

运行镜像仍为 `ix-lotusboard:php84-20261109`，镜像 ID：
`sha256:38dceadc913be10a6b03285878c1dccbf5d7cb3b374325f27ecf68dc41101529`。
本次按角色复用该镜像，没有重建镜像。实际 cron 的 PATH 与环境修复由
`bluegreen/crontab` 提供；SakuraNeko entrypoint 同步补上 PATH，供后续构建使用。

## 验收结果

| 项目 | 结果 |
| --- | --- |
| PHP 单元回归 | 10 tests，64 assertions 通过 |
| 新槽位 / 网关运行检查 | 每次 56 项通过 |
| 最终蓝→绿→蓝实测 | 三个入口共 940 次请求，0 失败 |
| 入口分布 | 本机 :84 676 次，公网 Nginx 132 次，公网隧道 132 次 |
| 在途慢速响应 | 两次各 4,688,029 字节，约 71.8 秒，SHA-256 均一致 |
| 调度实际执行 | 两槽位均观察到自动执行命令；最终心跳年龄 29 秒 |
| 队列积压 | traffic_fetch=0，send_email=0 |
| 配置共享 | 两个 FPM 池均缓存框架、不缓存共享后台/主题设置 |
| 代码核对 | source、blue、green 的 9 个修改文件均匹配提交 |
| 旧 PHP 回退保护 | 已实测拒绝直接操作固定网关 |

前一轮同样完成蓝→绿→蓝，936 次请求 0 失败。最后一轮额外验证了修正后的 cron
环境、启动后的真实调度心跳及后台角色交接。

首次接管端口时，本机探针 177 次采样中出现 18 次失败，首末失败采样间隔约 3.426 秒。
这一步存在短暂切换窗口；后续应用切换/回退的上述样本未观察到请求失败，不能据此
承诺任意负载、任意长响应下绝对零中断。

## 一并修复

- PHP 镜像默认 SIGQUIT 与 scheduler 关停脚本不匹配：Compose 和发布脚本显式使用 TERM，等待任务排空。
- Debian cron 缺少 PHP 路径和 Docker 缓存环境：专用 crontab 声明绝对路径和缓存环境；健康检查要求心跳晚于容器启动且保持新鲜。
- 后台保存设置生成槽位私有配置缓存：蓝绿部署清理配置缓存，普通部署保留原行为。
- 新主题初始化原有无限等待：写入配置后直接更新当前请求的主题配置。

应用提交：`3ee91b05`；运行环境源码提交：`621a96a`。发布工具和操作说明在本仓库。

## 证据和范围

服务器 `/root/lotusboard-docker-new/bluegreen/` 保留：

- `migration-evidence.json`：首次接管采样。
- `exercise-first-evidence.json`、`exercise-evidence.json`：两轮切换与慢速响应验证。
- `final-evidence.json`：最终角色状态、调度、队列及回退保护。
- `verification-*.json`：运行检查明细。
- `backups/`、`releases/`、`state.json`：迁移快照、发布文件与活动槽位。

证据、运行目录及状态不入 Git。没有执行真实购买、邮件发送或完整交互登录；支付验证
使用合成签名和回调。Laravel 仍为 8.83.29，后续框架升级另行处理。
