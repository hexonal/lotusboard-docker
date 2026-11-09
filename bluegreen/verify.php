<?php
require '/www/vendor/autoload.php';
$app = require '/www/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$checks = [];
function checked($condition, $label, array $detail = []) {
    global $checks;
    $checks[] = array_merge(['check' => $label, 'result' => $condition ? 'PASS' : 'FAIL'], $detail);
    if (!$condition) throw new RuntimeException('Runtime check failed: ' . $label);
}
function httpRequest($path, array $query = [], $post = null) {
    $base = getenv('RUNTIME_HTTP_BASE') ?: 'http://127.0.0.1:80';
    $ch = curl_init(rtrim($base, '/') . $path . ($query ? '?' . http_build_query($query) : ''));
    $headers = ['Host: v2-proxy.996icu.wiki', 'User-Agent: ix-php84-validation', 'Accept: application/json'];
    if ($post !== null) {
        curl_setopt($ch, CURLOPT_POST, true);
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($post));
        $headers[] = 'Content-Type: application/json';
    }
    curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 20, CURLOPT_HTTPHEADER => $headers]);
    $body = curl_exec($ch);
    $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $error = curl_errno($ch);
    curl_close($ch);
    if ($body === false) throw new RuntimeException('HTTP transport failure, errno=' . $error);
    return [$status, $body];
}
try {
    if (filter_var(getenv('APP_DISABLE_CONFIG_CACHE'), FILTER_VALIDATE_BOOLEAN)) {
        checked(class_exists(App\Utils\ConfigCache::class), 'shared-settings cache adapter');
        checked(!$app->configurationIsCached(), 'shared-settings read without frozen config cache');
    }
    checked(PHP_VERSION_ID >= 80400 && PHP_VERSION_ID < 80500, 'PHP 8.4', ['version' => PHP_VERSION]);
    foreach (['bcmath', 'curl', 'dom', 'gd', 'gmp', 'igbinary', 'intl', 'mbstring', 'mysqli', 'openssl', 'pcntl', 'pdo_mysql', 'pdo_sqlite', 'redis', 'sodium', 'sockets', 'sysvsem', 'xml', 'xmlreader', 'xmlwriter', 'zip'] as $ext) {
        checked(extension_loaded($ext), 'extension:' . $ext);
    }
    checked((int)Illuminate\Support\Facades\DB::selectOne('SELECT 1 AS n')->n === 1, 'database SELECT');
    $redis = Illuminate\Support\Facades\Redis::connection();
    checked((bool)$redis->ping(), 'existing Redis connectivity');
    $key = 'ix:php84:probe:' . bin2hex(random_bytes(8));
    try {
        $redis->setex($key, 30, 'runtime-ok');
        checked($redis->get($key) === 'runtime-ok', 'isolated Redis write/read');
    } finally {
        $redis->del($key);
    }
    $size = $app['queue']->connection('redis')->size('traffic_fetch');
    checked(is_int($size) && $size >= 0, 'queue access', ['traffic_fetch_jobs' => $size]);
    $hash = Illuminate\Support\Facades\Hash::make('runtime-probe-password');
    checked(Illuminate\Support\Facades\Hash::check('runtime-probe-password', $hash), 'password hashing');
    $jwt = Firebase\JWT\JWT::encode(['probe' => 'runtime-ok', 'exp' => time() + 60], 'runtime-probe-signing-key-only', 'HS256');
    checked(Firebase\JWT\JWT::decode($jwt, new Firebase\JWT\Key('runtime-probe-signing-key-only', 'HS256'))->probe === 'runtime-ok', 'JWT signing and verification');
    $ciphertext = Illuminate\Support\Facades\Crypt::encryptString('runtime-probe');
    checked(Illuminate\Support\Facades\Crypt::decryptString($ciphertext) === 'runtime-probe', 'application encryption');
    $markdown = new League\CommonMark\CommonMarkConverter(['html_input' => 'strip', 'allow_unsafe_links' => false]);
    checked(strpos((string)$markdown->convert('# Runtime probe'), '<h1>Runtime probe</h1>') !== false, 'Markdown and Nette schema configuration');
    $mail = (new Swift_Message('Runtime probe'))->setFrom('probe@example.com')->setTo('recipient@example.com')->setBody('Runtime probe');
    checked(strpos($mail->toString(), 'Subject: Runtime probe') !== false, 'email MIME generation without sending');

    $payload = json_encode(['id' => 'evt_runtime_probe', 'object' => 'event', 'type' => 'checkout.session.completed', 'data' => ['object' => ['payment_status' => 'paid', 'client_reference_id' => 'runtime-probe', 'payment_intent' => 'pi_runtime_probe']]]);
    $secret = 'whsec_runtime_probe_only';
    $time = time();
    $signature = 't=' . $time . ',v1=' . hash_hmac('sha256', $time . '.' . $payload, $secret);
    $event = Stripe\Webhook::constructEvent($payload, $signature, $secret);
    checked($event->type === 'checkout.session.completed', 'Stripe valid webhook signature');
    $rejected = false;
    try { Stripe\Webhook::constructEvent($payload, $signature, 'wrong-runtime-probe-key'); }
    catch (Stripe\Exception\SignatureVerificationException $e) { $rejected = true; }
    checked($rejected, 'Stripe invalid webhook rejected');
    $previousRequest = $app['request'];
    $app->instance('request', Illuminate\Http\Request::create('/runtime-probe', 'POST', [], [], [], [], $payload));
    $_SERVER['HTTP_STRIPE_SIGNATURE'] = $signature;
    $gateway = new App\Payments\StripeCheckout(['stripe_sk_live' => 'sk_test_runtime_probe_only', 'stripe_webhook_key' => $secret]);
    $result = $gateway->notify([]);
    checked($result['trade_no'] === 'runtime-probe', 'application Stripe webhook parsing');
    $app->instance('request', $previousRequest);
    unset($_SERVER['HTTP_STRIPE_SIGNATURE']);

    list($status) = httpRequest('/api/v1/guest/comm/config');
    checked($status === 200, 'HTTP public configuration', ['http_status' => $status]);
    list($status) = httpRequest('/api/v1/passport/auth/login', [], []);
    checked($status === 422, 'HTTP login input validation', ['http_status' => $status]);
    list($status) = httpRequest('/api/v1/client/app/getConfig');
    checked($status === 403, 'HTTP unauthenticated subscription denied', ['http_status' => $status]);

    if ((int)config('v2board.show_subscribe_method', 0) === 0) {
        $user = App\Models\User::where('banned', 0)->whereRaw('u + d < transfer_enable')->where(function ($query) {
            $query->whereNull('expired_at')->orWhere('expired_at', '>', time());
        })->orderBy('id')->first(['id', 'token']);
        checked($user && $user->token, 'eligible subscription account available');
        foreach (['app', 'meta', 'verge', 'nyanpasu', 'shadowrocket'] as $flag) {
            $path = $flag === 'app' ? '/api/v1/client/app/getConfig' : '/api/v1/client/subscribe';
            $query = ['token' => $user->token];
            if ($flag !== 'app') $query['flag'] = $flag;
            list($status, $body) = httpRequest($path, $query);
            checked($status === 200, 'HTTP subscription:' . $flag, ['http_status' => $status]);
            if ($flag === 'shadowrocket') {
                checked(strpos($body, '#!MANAGED-CONFIG') === 0 && strpos($body, '=vless,') !== false, 'Shadowrocket rules and VLESS');
                continue;
            }
            $config = Symfony\Component\Yaml\Yaml::parse($body);
            $types = array_column($config['proxies'], 'type');
            checked(count($types) > 0 && count(array_filter($types, function ($t) { return $t !== 'vless'; })) === 0, 'VLESS nodes:' . $flag, ['nodes' => count($types)]);
            $allowed = array_merge(['DIRECT', 'REJECT'], array_column($config['proxies'], 'name'), array_column($config['proxy-groups'], 'name'));
            foreach ($config['proxy-groups'] as $group) foreach ($group['proxies'] as $member) {
                if (!in_array($member, $allowed, true)) throw new RuntimeException('Invalid group reference in ' . $flag);
            }
            foreach ($config['rules'] as $rule) {
                $parts = explode(',', $rule);
                if ($parts[0] === 'RULE-SET' && !isset($config['rule-providers'][$parts[1]])) throw new RuntimeException('Invalid provider reference in ' . $flag);
            }
            checked(true, 'subscription references:' . $flag, ['rules' => count($config['rules']), 'groups' => count($config['proxy-groups'])]);
        }
    }
    $node = App\Models\ServerV2node::where('show', 0)->orderBy('id')->first(['id']);
    if ($node) {
        list($status, $body) = httpRequest('/api/v2/server/config', ['node_id' => $node->id, 'token' => config('v2board.server_token')]);
        checked($status === 200, 'HTTP backend node configuration', ['http_status' => $status]);
        $config = json_decode($body, true);
        checked(isset($config['protocol'], $config['server_port'], $config['tls_settings']), 'backend node response structure');
    }
    echo json_encode(['php' => PHP_VERSION, 'framework' => $app->version(), 'result' => 'PASS', 'checks' => $checks], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
} catch (Throwable $e) {
    echo json_encode(['php' => PHP_VERSION, 'result' => 'FAIL', 'error_class' => get_class($e), 'error' => $e instanceof RuntimeException && strpos($e->getMessage(), 'Runtime check failed: ') === 0 ? $e->getMessage() : 'Runtime check aborted; see error_class and completed checks', 'checks' => $checks], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
    exit(1);
}
