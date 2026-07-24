"""
AI 热门板块 —— DeepSeek 提示词模板
====================================

两段式调用（对应"先选板块、再从板块里选强势股"）：
  1. `sector_messages(pick_date, board_snapshot)` → 3 个热门板块
  2. `stock_messages(pick_date, sector_names, board_lookup)` → 每个板块 3 只最强正股

两次调用都要求严格 JSON 输出（DeepSeek `response_format={"type":"json_object"}`），
避免用正则解析自然语言。

v3：喂真实当日板块涨跌幅快照（见 runner._fetch_board_snapshot），选板块从"只能
凭训练记忆里的固定印象"改成"必须从今日真实数据里选" —— v2 明确禁止模型接触任何
当日行情数字，副作用是每天 prompt 里唯一变化的只有日期字符串，模型没有任何日期
相关信号可用，于是每天都收敛到同一批"确定性强主题"（人工智能/半导体常年 100%
命中）。v3 反过来：把当日真实涨跌幅数据喂给它，明确"只能从这份名单里选"，
从根上让推荐随当天实际行情变化。
"""
from __future__ import annotations

from datetime import date as _Date
from typing import Any, Dict, List, Optional

SECTOR_PROMPT_VERSION = "v3"
STOCK_PROMPT_VERSION = "v3"


def sector_messages(pick_date: _Date, board_snapshot: List[Dict[str, Any]]) -> List[dict]:
    system = (
        "你是一位专注中国A股市场的行业研究员，风格严谨、绝不编造事实。"
        "下面 user 消息会给你一份今日（已收盘）真实的板块涨跌幅快照——"
        "数据来自本站行情源，涨跌幅/领涨股都是真实值，不是你的记忆或猜测。"
        "你只能从这份名单里选板块，不允许选名单之外的板块名称；"
        "reason 里如果提到涨跌幅数字，必须和名单里给出的数字一致，不能编造名单外的数字，"
        "但可以结合你已掌握的产业趋势、政策方向等背景知识解释该板块今天为什么走强。"
        "只输出严格的 JSON，不要输出任何 JSON 之外的文字，不要用 markdown 代码块包裹。"
    )
    board_lines = "\n".join(
        f"- {b['name']}：涨跌幅 {b['pct_change']:+.2f}%"
        + (f"，领涨股 {b['leader']}（{b['leader_pct']:+.2f}%）" if b.get("leader") else "")
        for b in board_snapshot
    )
    user = (
        f"背景：今天是 {pick_date.isoformat()}（A 股交易日），已收盘。\n"
        f"以下是今日真实的板块涨跌幅快照，按涨幅从高到低排列（共 {len(board_snapshot)} 个）：\n"
        f"{board_lines}\n\n"
        "请从上面名单中选出你认为最值得关注的 3 个板块，按关注度从高到低排序。\n"
        "reason 请结合板块今日的真实涨跌幅表现，加上产业趋势/政策方向等背景解释入选理由，20-50字。\n\n"
        "严格按以下 JSON 格式输出，不要有多余文字：\n"
        '{"sectors": [{"name": "板块名称(必须与上面名单中的某一项完全一致)", '
        '"reason": "20-50字入选理由,可引用上面给出的真实涨跌幅数字"}, '
        '... 恰好3个]}'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def stock_messages(
    pick_date: _Date,
    sector_names: List[str],
    board_lookup: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[dict]:
    system = (
        "你是一位专注中国A股市场的选股研究员，风格严谨、绝不编造事实。"
        "重要限制：本次对话没有联网/实时行情能力，你看不到今天的任何个股盘面数据"
        "（涨跌幅、是否涨停、成交量、资金流向等一概不知道），除非 user 消息里明确给出。"
        "选股和写理由时只能依据你已掌握的、相对稳定的事实"
        "——公司在该板块的产业地位、主营业务、技术布局、历史市场认知度等，"
        "或 user 消息里明确给出的今日领涨股数据——"
        "绝对不允许编造 user 消息之外的具体当日行情数字（如「今日涨停」「涨超8%」之类），"
        "一旦无法确认就不要写这类描述，改写公司本身的基本面/产业逻辑。\n"
        "选股范围限制：只能给出真实存在、目前正常上市交易的A股代码"
        "（沪深京任意板块均可，但排除ST/*ST/退市整理期/已停牌股票），"
        "代码必须是6位数字且与公司名称真实对应；如果记不清某公司的准确代码，"
        "宁可换一只你更确定的股票，也不要编造代码。"
        "同一只股票不允许在不同板块重复出现。"
        "只输出严格的 JSON，不要输出任何 JSON 之外的文字，不要用 markdown 代码块包裹。"
    )
    sectors_str = "、".join(sector_names)
    leader_lines = []
    for name in sector_names:
        b = (board_lookup or {}).get(name)
        if b and b.get("leader"):
            leader_lines.append(f"- {name}：今日真实领涨股 {b['leader']}（{b['leader_pct']:+.2f}%）")
    leader_block = (
        "以下是这些板块今日真实的领涨股（可以作为入选依据之一，但不强制只能选它）：\n"
        + "\n".join(leader_lines) + "\n\n"
        if leader_lines else ""
    )
    user = (
        f"背景：今天是 {pick_date.isoformat()}（A 股交易日），已收盘。\n"
        f"{leader_block}"
        f"针对以下 3 个板块：{sectors_str}，"
        "请分别给出每个板块内最具代表性、最能体现该板块投资逻辑的 3 只正股，"
        "按你判断的代表性/龙头程度从高到低排序。\n"
        "reason 请基于产业地位、核心业务、技术卡位等可考证的信息展开，20-50字，"
        "不得包含任何具体的当日涨跌幅/是否涨停/资金流向等你无法确认的数据"
        "（上面给出的领涨股涨跌幅除外）。\n\n"
        "严格按以下 JSON 格式输出，不要有多余文字：\n"
        '{"sectors": ['
        '{"name": "板块名称(必须与给定板块名一致)", '
        '"stocks": [{"code": "6位数字股票代码", "name": "与代码真实对应的股票名称", '
        '"reason": "20-50字入选理由,不含当日行情数据(领涨股涨跌幅除外)"}, ... 恰好3个]}, '
        "... 覆盖全部给定板块，每个板块都必须给满3只]}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
