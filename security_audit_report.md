# 安全审计报告与加固方案 — A 股量化回测平台

> 审计角色：安全工程师（应用安全 / 威胁建模 / 安全加固）
> 审计对象：`D:\backtest_platform`（FastAPI + MySQL + 前端静态资源）
> 审计方式：静态代码审计 + 威胁建模（STRIDE）+ 配置/依赖核查
> 日期：2026-07-24（原始审计）
> 最近更新：2026-07-28 —— §3.3 / §3.4 的全部待办项均已完成，新增 §3.7 部署面暴露。
> **改动本文件的人请同步更新各项状态**，过期的安全文档比没有更危险。

---

## 1. 总体结论

这套代码的安全水位**明显高于同体量个人/小团队项目**：数据库访问全部参数化、会话令牌用 `secrets.token_urlsafe` 且库内只存 `sha256`、短信验证码有多维限流、管理员写操作有 IP 白名单 + 跨站请求校验、`X-Forwarded-For` 防伪造、SSE 流做了控制字符清洗、前端对模型生成文本与动态数据均做了 HTML 转义。

但依然存在若干**真实可利用的风险**，其中两项在高危部署场景（公网 / 反向代理）下可造成管理员权限被夺取。本次已**直接修复**高/中危项，低危项给出加固建议。

| 等级 | 数量 | 状态 |
|---|---|---|
| Critical | 0 | — |
| High | 2（配置/部署相关） | 1 项已代码修复，1 项（明文密钥）需人工轮换 |
| Medium | 4 | 全部已代码修复 |
| Low | 4 | 给出建议，部分可在后续迭代处理 |

---

## 2. 已修复项（本次提交）

### 2.1 管理员白名单「空表自举」越权（High → 已修复）
- **风险**：`app/paper_trading/admin_ip.py` 的 `require_admin_ip` 在白名单表为空时，会把**第一个**命中受保护端点的请求 IP 自动加进白名单并放行。公网部署且未设 `PAPER_ADMIN_INITIAL_IPS` 时，攻击者只要比管理员先访问一次 `/api/data_status/today`（router 级已挂该依赖），即可把自己变成管理员，进而触发 `subprocess` 任务、改策略参数、激活订单。
- **修复**：自举仅允许来自**内网/本机**（`_is_private_ip`）的首个请求；公网首个访问者直接收到 `403` 并提示用 `PAPER_ADMIN_INITIAL_IPS` 初始化。
- 文件：`app/paper_trading/admin_ip.py`（自举分支，约 293 行起）

### 2.2 反向代理下客户端 IP 失真（High → 已缓解 + 文档强化）
- **风险**：未配 `TRUSTED_PROXIES` 却部署在反代后时，所有请求的直连 IP 都是代理地址（如 `127.0.0.1`）。此时 2.1 的自举会把**代理 IP** 加进白名单，等于所有经该代理的流量都成了管理员；`X-Forwarded-For` 可被任意客户端伪造绕过白名单。
- **修复**：自举已限制为内网 IP（代理 IP 通常也是内网，故仍需正确配置 `TRUSTED_PROXIES`）。已在多处强调：生产反代**必须**设置 `TRUSTED_PROXIES`，否则白名单机制整体失效。

### 2.3 明文密钥落盘（High，配置层面 → 代码无法替你轮换）
- **风险**：仓库根目录 `.env` 含明文 `MYSQL_PASSWORD=lhc123456`（root 弱口令）与 `DEEPSEEK_API_KEY=sk-...`。虽已被 `.gitignore` 忽略（未进版本库），但躺在项目根目录、服务器一旦被入侵或误 `git add` 即泄露。
- **修复（需你执行）**：① 立即到 DeepSeek 控制台**吊销并轮换**该 Key；② 改 MySQL 为**专用低权限账号**，弃用 root；③ 密码改为强随机；④ `.env` 不进版本库（已满足），并考虑用密钥管理（Vault / 云 Secrets Manager）。**代码侧**已新增 `DEBUG` 开关避免排障时把 Key 误打进日志。

### 2.4 重型计算接口无频控（Medium → 已修复）
- **风险**：`/api/backtest`、`/api/portfolio_backtest` 公开、无鉴权、无速率限制，会触发回测 / 全市场行情下载等重计算，易被用于资源耗尽型 DoS。
- **修复**：新增 `_rate_limit_backtest`，限 **6 次/分钟/IP**（进程内滑动窗口）。命中即 `429`。
- 文件：`app/main.py`（`_rate_limit_backtest` + 两个端点 `Depends`）

### 2.5 错误信息泄露内部细节（Medium → 已修复）
- **风险**：`api_kline` / `api_backtest` 等用 `detail=str(e)`、SSE 错误用 `f"...：{e}"` 把数据库/akshare 内部异常直接回给客户端，可被用于探测表结构、内部路径。
- **修复**：新增 `_safe_detail(msg, exc)`，对外只返回通用文案；仅当环境变量 `DEBUG=1` 才附带原始异常（本地排障用）。
- 文件：`app/config.py`（新增 `DEBUG`）、`app/main.py`（计算类端点 + SSE 错误）

### 2.6 会话 Cookie `Secure` 标记可被伪造（Medium → 已修复）
- **风险**：`auth/api.py` 的 `_cookie_secure` 信任客户端可控的 `X-Forwarded-Proto` 头判断是否置 `Secure`，攻击者可让其降级为 http，使会话 cookie 经明文传输被嗅探。
- **修复**：仅在直连 IP 属于 `TRUSTED_PROXIES` 时才信任该头。
- 文件：`app/auth/api.py`

### 2.7 `run.py` 开发模式上生产（Medium → 已修复）
- **风险**：`uvicorn.run(..., reload=True)` 绑定 `0.0.0.0`，自动重载器是开发特性，会暴露重载端口、浪费资源，且 `0.0.0.0` 默认全网卡监听。
- **修复**：`reload` 默认关闭，仅 `RELOAD=1` 开启；`BIND_HOST` / `BIND_PORT` 环境变量化。公网部署务必配合防火墙或反代。
- 文件：`run.py`

### 2.8 缺失安全响应头（Medium → 已修复）
- **修复**：新增 `SecurityHeadersMiddleware`，统一下发 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy`、`Permissions-Policy`、严格 `Content-Security-Policy`（防 framing / 数据信道 / 限制外联脚本源），并在 HTTPS（含受信反代）下下发 `Strict-Transport-Security`。CSP 当前为兼容内联脚本的折中（`script-src 'self' 'unsafe-inline'`），后续可收紧（见 3.4）。
- 文件：`app/main.py`

### 2.9 `normalize_code` 不做格式校验（Low → 已修复）
- **风险**：`normalize_code` 仅做前缀剥离 + `zfill(6)`，可把任意字符串透传给 akshare / DB 查询。
- **修复**：增加 `^\d{6}$` 正则，非法 code 直接 `400`（如 `'; DROP TABLE--` 不再透传）。
- 文件：`app/data/data_loader.py`

---

## 3. 待办加固建议（需人工 / 后续迭代）

### 3.1 密钥与凭据（高优先级，人工）
- 轮换 DeepSeek Key、弃用 MySQL root、改用强随机密码与低权限账号。
- 在 CI 增加「防密钥提交」守卫（如 pre-commit + gitleaks），即便 `.gitignore` 已覆盖也作双保险。

### 3.2 反代部署必做
- 设置 `TRUSTED_PROXIES=127.0.0.1,10.0.0.0/8`（按实际反代网段），否则 2.1/2.2 的防护不成立。
- 反代层强制 HTTPS + `X-Forwarded-Proto: https`，使 HSTS 与 `Secure` Cookie 生效。

### 3.3 匿名写接口滥用 —— ✅ 已于 2026-07-28 全部完成
- ~~`/api/feedback`（匿名写库）增加频控 + 内容长度上限~~ → 已完成（`25d464d`）：
  `app/feedback/service.py` 加 `IP_MAX_PER_HOUR=5`、`MAX_CONTENT=2000`、`MAX_CONTACT=100`，
  超限抛 `FeedbackRateLimitError` → HTTP 429。CAPTCHA 暂未加，当前量级不需要。
- ~~`/api/my_board/layout` 访客可覆盖全站共享默认布局~~ → 已完成：按身份分流 ——
  已登录写自己那一行、自由保存；未登录写的是共享行（`GUEST_USER_ID=0`），
  要求来自管理白名单 IP。保留了「维护者未登录摆布局＝新访客默认视图」这一产品行为，
  同时挡住路人投毒。前端对 403 单独处理，不再误报「保存失败」。

### 3.4 纵深防御（防御性）—— ✅ 已于 2026-07-27 全部完成（`9500748`）
- ~~`traffic_today` 的 `{tcol}` 标识符拼接~~ → 已完成：`app/data_status/api.py` 加
  `_SAFE_IDENT_RE = ^[A-Za-z_][A-Za-z0-9_]{0,63}$` 白名单校验。
- ~~`market_data.py` 的 `subprocess` + `pickle` IPC~~ → 已完成：改为 CSV 明文
  （`to_csv` / `read_csv`），彻底移除反序列化执行面。未用 parquet 是因为生产机
  没装 pyarrow，CSV 零依赖且此处数据结构简单（4 列扁平表）。
- ~~CSP 收紧~~ → 已完成：全站 ~37 处内联 `onclick/onchange/oninput` 改为容器级
  事件委托，CSP 升级为 `script-src 'self'`（已去掉 `unsafe-inline`）。
  > 教训：该改动使 HTML 与 JS 变成必须成对更新，但当时漏 bump 三个 `?v=`
  > 缓存版本号，老用户会拿到「新 HTML + 旧 JS」导致整页按钮失灵。
  > 已在 `a37bb7a` 修复，并在 `app/main.py` 加了显式 Cache-Control 策略从根上消除
  > 对浏览器启发式缓存的依赖（见 §3.7）。

### 3.7 部署面暴露 —— ✅ 已于 2026-07-28 处理（本轮新发现）
- ~~uvicorn 绑定 `0.0.0.0:8000`，公网可直连绕过 nginx~~ → 已改为 `--host 127.0.0.1`。
  nginx 本来就是 `proxy_pass http://127.0.0.1:8000`，不受影响。
  绕过 nginx 意味着无 TLS、无反代层限流；实测 `X-Forwarded-For` 伪造**无效**
  （`_client_ip` 从右往左跳过可信代理的写法是对的），故非权限绕过，但仍属明文暴露。
- ~~`.env` 中 `MYSQL_HOST` 指向自己的公网 IP~~ → 已改为 `127.0.0.1`：
  此前每条 SQL 都在公网网卡上兜一圈，且 `require_secure_transport=OFF` 全程明文。
- `.env` 权限从 `644` 收紧为 `600`。
- **仍未处理（业务方决定保留）**：MySQL `bind_address=*` + `root@%` 账号 + 3306
  对公网 TCP 可达。维护者需要从本地直连生产库，故保留远程 root。
  当前唯一屏障是云厂商安全组（仓库外、不可见）。建议后续改为：安全组只放行
  维护者出口 IP，或改用 SSH 隧道 + `bind-address=127.0.0.1`。
- 同机的宝塔面板 `:8888`、phpMyAdmin 转发 `:888` 亦对公网监听，不在本仓库范围，
  建议至少做 IP 限制。

### 3.8 服务进程降权 —— ✅ 已于 2026-07-28 处理

原先 `backtest.service` 是 `User=root`、`WorkingDirectory=/backtest_platform`，
任何一个 RCE 直接等于拿到整机。已改为：

- 新建系统用户 `backtest`（`--system --no-create-home --shell nologin`），
  `chown -R backtest:backtest /backtest_platform`，`.env` 保持 `600`。
- `Environment=HOME=/backtest_platform` —— 该用户没有独立家目录，
  akshare 之类要写缓存时有地方落。
- 加固指令：`NoNewPrivileges` / `PrivateTmp` / `PrivateDevices` / `ProtectHome` /
  `ProtectSystem=full` / `ProtectKernelTunables` / `ProtectControlGroups` /
  `RestrictSUIDSGID`，配 `ReadWritePaths=/backtest_platform`。
- **同时把调度器的 crontab 一起迁了**：它原本挂在 root crontab
  （`*/5 * * * * python3 scripts/run_scheduled_tasks.py`），如果只改 systemd
  不改它，两边生成的日志/缓存文件属主会打架、必有一方写不动。
  现移到 `/etc/cron.d/backtest`，用户字段填 `backtest`。
- 顺带补 `/etc/logrotate.d/backtest`：项目自己写的 `scripts/*.log` 此前
  没有任何轮转（`scheduler.log` 已涨到 ~4MB 只增不减），改为按周切、留 8 份、
  `copytruncate`（写进程用 `>>` 追加，不会重开 fd）。

回滚材料：`/root/backtest.service.bak.20260728`、`/root/crontab.bak.20260728`、
`/backtest_platform/.env.bak.20260728`。

---

## 8. 仍然敞开的一项（需产品决策，非技术阻塞）

**管理白名单绑定在动态 IP 上。** `paper_admin_ip` 只认来源 IP，而维护者用的是
家庭宽带的动态 IP。ISP 回收并重新分配该 IP 后，**下一个拿到它的人自动继承管理员**，
可以改策略参数、触发 `subprocess` 任务、改全站默认布局。

几个方向，各有代价，需要业务方选：

1. **白名单 + 登录态双因子** —— 写操作要求「IP 在白名单」且「已登录」。最稳，
   但维护者每次都得先登录，且 `/admin/tasks` 这类页面的使用方式会变。
2. **条目自动过期** —— 加 `last_used_at`，超过 N 天未使用的条目自动失效。
   自维护、无需 UI；但维护者长时间不用会被锁在门外，只能靠直连数据库恢复
   （目前保留了 root@% 远程访问，恢复路径是通的）。
3. **维持现状** —— 接受风险。前提是维护者的出口 IP 相对稳定，且能及时发现异常。

本轮**未擅自改动**，因为三个方向都会改变维护者日常的操作方式，属于产品决策。

### 3.5 CI/CD 安全流水线
- 已随本报告附带 `.github/workflows/security-scan.yml`：SAST（Semgrep OWASP/CWE）、SCA（Trivy）、密钥扫描（Gitleaks）。任何 PR 合并前必须通过这些扫描。

### 3.6 运行时监控与告警
- 对以下事件加告警：白名单 IP 增删、`/api/admin/ip/me` 异常高频、登录失败突增、`/api/backtest` 触发 `429` 突增。

---

## 4. 验证

- 已对本次修改的 6 个文件执行 `python -m py_compile`，**全部通过**。
- 新增符号（`_safe_detail` / `_rate_limit_backtest` / `SecurityHeadersMiddleware` / `_is_https_request` / `_is_private_ip` 引用 / `_is_from_trusted_proxy` 引用）均已 grep 确认正确接入。
- 注意：`py_compile` 仅验证语法；完整功能回归需在依赖就绪环境运行 `pytest` 与一次真实启动冒烟。

---

## 5. 附录 A — 威胁模型摘要（STRIDE）

| 威胁 | 组件 | 风险 | 现状 / 缓解 |
|---|---|---|---|
| Spoofing | 管理员写接口 | 高 | IP 白名单 + 跨站校验；自举已限内网（2.1/2.2） |
| Tampering | API 请求 / 参数 | 中 | 全参数化查询；`normalize_code` 校验（2.9） |
| Reputation | 用户操作 | 低 | 会话表 + 访问日志（`user_visit_log`） |
| Info Disclosure | 错误响应 / 日志 | 中 | `_safe_detail` 通用化（2.5）；密钥不落日志 |
| DoS | 回测计算接口 | 中 | 速率限制（2.4）；日期跨度上限已存在 |
| Elevation of Priv | 公网首个访问者 | 高 | 自举越权已修复（2.1） |

---

## 6. 附录 B — 推荐 nginx 安全头片段（反代层）

```nginx
server {
    listen 443 ssl;
    server_name your.domain;

    # 反代到 uvicorn(127.0.0.1:8000)，并正确传递协议与客户端 IP
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;

    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
    add_header Content-Security-Policy "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self';" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;

    server_tokens off;
}
```

## 7. 附录 C — 已修改文件清单

| 文件 | 改动 |
|---|---|
| `run.py` | 默认关闭 `reload`；`BIND_HOST`/`BIND_PORT`/`RELOAD` 环境变量化 |
| `app/config.py` | 新增 `DEBUG` 开关 |
| `app/main.py` | 安全响应头中间件、`_safe_detail`、`_rate_limit_backtest`、三个计算端点接入限流与脱敏、股票代码校验 |
| `app/paper_trading/admin_ip.py` | 自举仅限内网/本机，否则 403 |
| `app/auth/api.py` | `Secure` Cookie 仅受信反代下信任 `X-Forwarded-Proto` |
| `app/data/data_loader.py` | `normalize_code` 增加 `^\d{6}$` 校验 |
| `.github/workflows/security-scan.yml`（新增） | SAST + SCA + 密钥扫描 CI |
