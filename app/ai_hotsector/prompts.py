"""
AI 板块热度观察 —— DeepSeek 提示词模板
======================================

单段式调用：`sector_messages(pick_date, board_snapshot)`。

合规方案 B：产品已去掉个股选择性 —— 当日 Top 3 板块由**客观涨跌幅排序**决定
（runner 抓当日真实板块涨跌幅快照后直接取前 3），**不由 AI 挑选**。AI 只对
给定的这几个板块各写一段**客观观察记录**，描述当日真实涨跌幅/量能与产业背景，
不得出现任何推荐、评价或方向性措辞。

严禁模型编造当日行情数字：提示词里喂真实快照，name 必须与给定名称完全一致，
note 里引用的涨跌幅必须与快照一致（这是 v3 起就有的约束，继续沿用）。

v4：角色从"选板块"改为"只做客观观察记录"，输出结构由
{"sectors":[{"name","reason"}]} 变为 {"sectors":[{"name","note"}]}；
推荐 / 预测性措辞禁用清单（REASON_BANNED_WORDS）继续沿用，展示前由
api.public_reason() 再过滤一遍。**不再有第二段"从板块里挑强势个股"的调用**
（原 stock_messages / STOCK_PROMPT_VERSION 已随方案 B 删除）。
"""
from __future__ import annotations

from datetime import date as _Date
from typing import Any, Dict, List

SECTOR_PROMPT_VERSION = "v4"

# note 里禁止出现的推荐 / 预测性措辞(写进提示词;展示前 api.public_reason() 也会再查一遍)
REASON_BANNED_WORDS = (
    "建议", "买入", "卖出", "看好", "看涨", "看空", "机会", "上涨空间", "目标价",
    "值得关注", "值得配置", "推荐", "龙头", "强势", "受益", "有望", "弹性",
    "必涨", "翻倍",
)


def sector_messages(pick_date: _Date, board_snapshot: List[Dict[str, Any]]) -> List[dict]:
    """给定**客观选定**的当日 Top 板块（按真实涨跌幅降序取前几个，由 runner 决定），
    要求模型对每一个板块各写一段 20-50 字客观观察记录。

    返回严格 JSON：{"sectors":[{"name":"与给定名称完全一致","note":"客观观察记录"}]}。
    """
    system = (
        "你是一位专注中国A股市场的行业研究员，风格严谨、绝不编造事实。"
        "下面 user 消息会给你一份今日（已收盘）真实的板块涨跌幅快照——"
        "数据来自本站行情源，涨跌幅/领涨股都是真实值，不是你的记忆或猜测。"
        "这份名单已经由当日真实涨跌幅客观排序确定，你的任务不是挑选，"
        "而是对名单里**给定的每一个板块**各写一段客观观察记录。"
        "note 里如果提到涨跌幅数字，必须和名单里给出的数字一致，不能编造名单外的数字，"
        "但可以结合你已掌握的产业趋势、政策方向等背景知识做客观描述。"
        "只输出严格的 JSON，不要输出任何 JSON 之外的文字，不要用 markdown 代码块包裹。"
    )
    board_lines = "\n".join(
        f"- {b['name']}：涨跌幅 {b['pct_change']:+.2f}%"
        + (f"，领涨股 {b['leader']}（{b['leader_pct']:+.2f}%）" if b.get("leader") else "")
        for b in board_snapshot
    )
    user = (
        f"背景：今天是 {pick_date.isoformat()}（A 股交易日），已收盘。\n"
        f"以下是今日真实的板块涨跌幅快照，已按涨幅从高到低客观排序（共 {len(board_snapshot)} 个）：\n"
        f"{board_lines}\n\n"
        "请对上面名单里的**每一个板块**各写一段客观观察记录（20-50字）："
        "描述它当日真实的涨跌幅/量能表现，并结合产业趋势、政策方向等背景做客观说明。\n"
        "这段文字是给读者看的客观记录，不是推荐：不得出现任何推荐、评价好坏或"
        "预测涨跌的措辞，包括但不限于"
        + "『" + "』『".join(REASON_BANNED_WORDS) + "』。\n\n"
        "严格按以下 JSON 格式输出，不要有多余文字：\n"
        '{"sectors": [{"name": "板块名称(必须与上面名单中的某一项完全一致)", '
        '"note": "20-50字客观观察记录,不含推荐/预测措辞"}, '
        "... 覆盖上面名单里的全部板块]}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
