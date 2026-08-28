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
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    # 调试开关:开启后对外错误信息会附带内部异常细节(仅本地排障用,生产务必关闭)
    DEBUG: bool = os.getenv("DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")

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
