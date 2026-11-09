<?php
require '/www/vendor/autoload.php';
$app = require '/www/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$heartbeat = Illuminate\Support\Facades\Cache::get(App\Utils\CacheKey::get('SCHEDULE_LAST_CHECK_AT', null));
$pid = @file_get_contents('/tmp/lotusboard-cron.pid');
$started = (int) @file_get_contents('/tmp/scheduler-started-at');
exit($pid && is_dir('/proc/' . trim($pid)) && $started && $heartbeat >= $started && time() - $heartbeat < 90 ? 0 : 1);
