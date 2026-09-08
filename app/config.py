import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)


def parse_admin_emails(raw: str) -> frozenset:
    """"a@qq.com, B@QQ.com" → {"a@qq.com", "b@qq.com"}。

    拆成独立函数是为了能单测:Settings 的字段在**类体执行时**求值(整个模块只跑
    一次)，测试里改环境变量再 new 一个 Settings() 是拿不到新值的。
    """
    return frozenset(
        e.strip().lower() for e in (raw or "").split(",") if e.strip()
    )


class Settings:
    MYSQL_HOST: str = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
    MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "")
    # ── LLM(OpenAI 兼容协议) ─────────────────────────────────────────
    # 三个 AI 功能共用一个出口(app/llm_client.py)。换供应商 = 改这几个变量，
    # 不动代码 —— DeepSeek / 智谱 / 硅基流动 / 百炼 / 火山用的是同一套协议。
    #
    #   LLM_PROVIDER  预置档案名(deepseek / zhipu / custom)
    #   LLM_BASE_URL  留空用档案默认;要接档案里没有的家才需要给
    #   LLM_MODEL     留空用档案默认(如智谱的 glm-4-flash)
    #   LLM_API_KEY   显式指定;留空则按 provider 取下面对应的那把
    #
    # Key 按家分开存，是为了"两家的 Key 都留着、改一行 LLM_PROVIDER 就切回去"——
    # 共用一个变量的话，切换时容易换了 endpoint 忘了换 Key。
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "").strip()
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "").strip()
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
    ZHIPU_API_KEY: str = os.getenv("ZHIPU_API_KEY", "").strip()

    # 三个 AI 任务的负载差了一个量级，共用一个模型必然有一头受委屈：
    #   个股报告  每天 50 次连续调用 —— 要快、要扛得住限流
    #   每日复盘  每天 1 次，写作质量最要紧 —— 慢一点无所谓
    #   热门板块  每天 2 次，其中"选股"那次是全站最重的一次调用
    # 留空则回落到全局 LLM_MODEL。
    LLM_MODEL_REPORT: str = os.getenv("LLM_MODEL_REPORT", "").strip()
    LLM_MODEL_REVIEW: str = os.getenv("LLM_MODEL_REVIEW", "").strip()
    LLM_MODEL_HOTSECTOR: str = os.getenv("LLM_MODEL_HOTSECTOR", "").strip()
    # 调试开关:开启后对外错误信息会附带内部异常细节(仅本地排障用,生产务必关闭)
    DEBUG: bool = os.getenv("DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")

    # 站点对外地址(带协议，不带结尾斜杠)。canonical / sitemap / 邮件里的链接
    # 都从这里取 —— 邮件里的链接必须是绝对地址，相对路径在邮件客户端里点不动。
    SITE_ORIGIN: str = os.getenv("SITE_ORIGIN", "https://shoupan.asia").rstrip("/")

    # 管理员账号(登录邮箱，逗号分隔，大小写不敏感)。
    #
    # 刻意放在 .env 而不是数据库：管理员名单是**能不能改数据**的总开关，
    # 放库里就意味着"拿到一次 SQL 注入或库口令 = 拿到管理权"。放配置文件里
    # 则要先拿到服务器文件系统，门槛完全不同。代价是加人要改 .env 重启，
    # 但这个站点的管理员本来就只有一个。
    #
    # 留空 = 全站没有管理员(所有管理接口一律 403)。这是有意的安全默认：
    # 漏配的后果是"我进不去"，而不是"谁都能进"。
    ADMIN_EMAILS: frozenset = parse_admin_emails(os.getenv("ADMIN_EMAILS", ""))


settings = Settings()
