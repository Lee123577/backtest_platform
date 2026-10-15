"""
爬虫识别(纯函数,零 app 内依赖)
================================

**为什么单独一个文件、且不 import 任何 app 模块**:这个判定要同时被
`analytics`(事件表打标)和 `visit_log`(访问日志打标)调用,后者是最外层
中间件、在 main.py 里比 auth 更早加载 —— 一旦这里 import 了 analytics.db 或
auth,就会把整条依赖链在启动早期拽起来,还容易绕出循环 import。所以它只依赖
标准库,谁都能安全地用。

**为什么"打标而不丢弃"**:真实搜索引擎爬虫(Googlebot / Baiduspider / bingbot)
必须保留 —— 它们是衡量 SEO 效果的唯一数据源,丢了就没法回答"百度到底有没有
来抓、抓了多少"。所以这里只判断"是不是爬虫、属于哪一类",分类结果写进
`is_bot` / `bot_kind`,由报表侧决定排除哪几类。

**为什么 UA 打标只是"尽力而为"**:UA 是请求方自己写的,伪装成本为零
(实测有大量伪装成 Mac Chrome 的无头抓取,UA 关键字根本拦不住)。唯一稳定的
真人信号是"浏览器执行了 JS"。所以 UA 打标只做第一层筛子,真人口径最终仍以
前端 `page_view`(爬虫不跑 JS)为准 —— 这里只保证"访问日志"和"事件表"用的是
**同一份 UA 规则**,消除两套正则各说各话的口径漂移。

规则带版本号(`RULES_VERSION`),写进事件表的 `app_version` 便于口径回溯。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# 规则版本:规则一改就 +1,便于在 `/api/analytics/funnel` 或 SQL 里按口径切分历史
RULES_VERSION = "1"

# ── 爬虫分类规则(判定顺序即元组顺序,首个命中即停)─────────────────────────
# 顺序很关键:`PetalBot` 既命中 search 也命中末尾 unknown 的 `bot`,按顺序先归
# search 更有用(它确实是搜索引擎);`BingPreview` 同理会先归 search。
_BOT_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "search",
        re.compile(
            r"Googlebot|Baiduspider|bingbot|BingPreview|YandexBot|"
            r"Sogou web spider|360Spider|YisouSpider|DuckDuckBot|Applebot|"
            r"PetalBot|HaoSouSpider",
            re.I,
        ),
    ),
    (
        "ai",
        re.compile(
            r"GPTBot|OAI-SearchBot|ChatGPT-User|ClaudeBot|Claude-Web|"
            r"anthropic-ai|PerplexityBot|Google-Extended|Applebot-Extended|"
            r"Bytespider|CCBot|Amazonbot",
            re.I,
        ),
    ),
    (
        "seo",
        re.compile(
            r"SemrushBot|AhrefsBot|MJ12bot|DotBot|DataForSeoBot|SRanking|"
            r"GenomeCrawler|backlink",
            re.I,
        ),
    ),
    (
        "scanner",
        # `http://` / `https://` 是"把 URL 当 UA"的扫描器(实测 /wp-admin 扫描器
        # 直接把自己的目标 URL 塞进 UA 头),`curl/` `python-requests` 等是脚本。
        re.compile(
            r"libredtail-http|masscan|zgrab|Nuclei|sqlmap|python-requests|"
            r"curl/|Go-http-client|http://|https://",
            re.I,
        ),
    ),
    (
        "unknown",
        re.compile(r"bot|crawler|spider|slurp|bingpreview|headless|phantomjs|puppeteer", re.I),
    ),
)


def classify(ua: Optional[str]) -> Tuple[bool, Optional[str]]:
    """按 UA 判 (is_bot, bot_kind)。都不命中 → (False, None)。

    判定顺序 search → ai → seo → scanner → unknown,首个命中即停。
    """
    if not ua:
        return False, None
    for kind, pattern in _BOT_RULES:
        if pattern.search(ua):
            return True, kind
    return False, None


# ── UA 解析三件套(device_type / os / browser)───────────────────────────────
# 逻辑与 visit_log 原先的 _parse_ua 完全一致,只是搬到这里做单一事实来源。
# 唯一的改动:device 的爬虫判定改为调用本模块的 classify(),这样"访问日志说
# 是 bot"与"事件表 is_bot=1"永远同源,不会再出现两套正则口径打架。
# 注意 device_type='bot' 这个既有取值被运维页的对照查询依赖,不能改。
_OS_PATTERNS = [
    ("iOS",     re.compile(r"iPhone|iPad|iPod", re.I)),     # iOS 要先于 macOS 匹配
    ("Android", re.compile(r"Android", re.I)),
    ("Windows", re.compile(r"Windows NT", re.I)),
    ("macOS",   re.compile(r"Mac OS X|Macintosh", re.I)),
    ("Linux",   re.compile(r"Linux", re.I)),
]
_BROWSER_PATTERNS = [
    ("Edge",    re.compile(r"Edg/", re.I)),                  # Edge 先于 Chrome
    ("Chrome",  re.compile(r"Chrome/", re.I)),
    ("Firefox", re.compile(r"Firefox/", re.I)),
    ("Safari",  re.compile(r"Safari/", re.I)),               # Safari 最后(Chrome 也含 Safari 字样)
    ("IE",      re.compile(r"MSIE|Trident", re.I)),
]
_MOBILE_PATTERN = re.compile(r"Mobile|Android|iPhone|iPad|iPod", re.I)


def parse_ua(ua: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """UA → (device_type, os, browser)。

    device_type ∈ {bot, mobile, pc, None};bot 判定与 `classify()` 同源。
    """
    if not ua:
        return None, None, None
    os_name = next((n for n, p in _OS_PATTERNS if p.search(ua)), None)
    browser = next((n for n, p in _BROWSER_PATTERNS if p.search(ua)), None)
    if classify(ua)[0]:
        device = "bot"
    elif _MOBILE_PATTERN.search(ua):
        device = "mobile"
    else:
        device = "pc"
    return device, os_name, browser
