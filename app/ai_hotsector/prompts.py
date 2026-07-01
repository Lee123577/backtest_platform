"""
AI 热门板块 —— DeepSeek 提示词模板
====================================

两段式调用（对应"先选板块、再从板块里选强势股"）：
  1. `sector_messages(pick_date)`               → 3 个热门板块
  2. `stock_messages(pick_date, sector_names)`  → 每个板块 3 只最强正股

两次调用都要求严格 JSON 输出（DeepSeek `response_format={"type":"json_object"}`），
避免用正则解析自然语言。
"""
from __future__ import annotations

from datetime import date as _Date
from typing import List

SECTOR_PROMPT_VERSION = "v1"
STOCK_PROMPT_VERSION = "v1"


def sector_messages(pick_date: _Date) -> List[dict]:
    system = (
        "你是一位专注中国A股市场的短线行业研究员。"
        "你的任务是从当天收盘后的市场表现、资金动向、政策/新闻热度等角度，"
        "判断今天最值得关注的热门板块。只输出严格的 JSON，不要输出任何 JSON 之外的文字。"
    )
    user = (
        f"今天是 {pick_date.isoformat()}（A 股交易日），已收盘。"
        "请给出今天A股市场最热门的 3 个行业/概念板块（例如：人工智能、半导体、机器人、"
        "创新药、军工等具体行业或概念，不要用'大盘'这种笼统说法），"
        "按热度从高到低排序。\n\n"
        "严格按以下 JSON 格式输出，不要有多余文字：\n"
        '{"sectors": [{"name": "板块名称", "reason": "20-50字的入选理由"}, '
        '... 恰好3个]}'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def stock_messages(pick_date: _Date, sector_names: List[str]) -> List[dict]:
    system = (
        "你是一位专注中国A股市场的短线选股研究员。"
        "你的任务是在给定的行业/概念板块内，挑选当天(收盘后)最具代表性、最强势的正股。"
        "只能给出真实存在、目前仍在A股上市交易的股票代码（6位数字，如 600519、300750），"
        "不要虚构代码。只输出严格的 JSON，不要输出任何 JSON 之外的文字。"
    )
    sectors_str = "、".join(sector_names)
    user = (
        f"今天是 {pick_date.isoformat()}（A 股交易日），已收盘。"
        f"针对以下 3 个板块：{sectors_str}，"
        "请分别给出该板块内当前最强势（今天表现最好/最具代表性的龙头，且必须是真实存在的"
        "A股上市公司）的 3 只正股，按强弱从高到低排序。\n\n"
        "严格按以下 JSON 格式输出，不要有多余文字：\n"
        '{"sectors": ['
        '{"name": "板块名称(必须与给定板块名一致)", '
        '"stocks": [{"code": "6位股票代码", "name": "股票名称", '
        '"reason": "20-50字入选理由"}, ... 恰好3个]}, '
        "... 覆盖全部给定板块]}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
