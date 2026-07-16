"""
AI 每日复盘 —— DeepSeek 提示词模板
====================================

与 ai_hotsector 的关键差异：这里**给模型喂真实的当日数据**（指数涨跌、
涨跌家数、成交额、AI 选股结算结果），模型的任务是解读，不是回忆 ——
所以约束从"不许编造当日数字"变成"只许使用提供的当日数字"。

要求严格 JSON 输出（DeepSeek ``response_format={"type":"json_object"}``）：
  {"title": "复盘标题", "content_md": "markdown 正文"}
"""
from __future__ import annotations

import json
from datetime import date as _Date
from typing import Any, Dict, List

# v2: context 增加指数近60日位置(hi/lo/pos_60d)、5日均量、全市场涨跌幅榜 Top10
# v3: 增加行业板块快照(领涨领跌板块+领涨股)、连板梯队(近似口径);
#     正文从四节扩到五节(新增"板块聚焦"),字数 400~700
# v4: 板块数据源东财→新浪(东财封锁云机房 IP,"板块聚焦"一节在生产上从上线起
#     就没成功过);字段口径 up/down → stock_count
REVIEW_PROMPT_VERSION = "v4"


def review_messages(review_date: _Date, context: Dict[str, Any]) -> List[dict]:
    system = (
        "你是一位专注中国A股市场的复盘作者，风格严谨、克制、绝不编造事实。"
        "重要限制：本次对话没有联网/实时行情能力，关于当日行情你只能使用"
        "用户提供的数据，绝对不允许编造用户数据里没有的当日数字"
        "（个股涨跌幅、板块资金流向、北向资金、具体涨停家数等一概不许虚构）。"
        "可以结合你已掌握的、相对稳定的产业趋势/政策背景做定性解读，"
        "但不得虚构当日发生的具体事件或新闻。"
        "只输出严格的 JSON，不要输出任何 JSON 之外的文字，"
        "不要用 markdown 代码块包裹 JSON。"
    )
    data_str = json.dumps(context, ensure_ascii=False, indent=1)
    user = (
        f"背景：今天是 {review_date.isoformat()}（A 股交易日），已收盘。"
        "以下是当日真实市场数据（JSON）。\n"
        "字段说明：\n"
        "- indices：主要指数，close=收盘点位，pct_change=当日涨跌幅(%)，"
        "hi_60d/lo_60d=近60个交易日收盘最高/最低点位，"
        "pos_60d_pct=当日收盘在该区间的位置(0=区间最低,100=区间最高，"
        "可据此写'逼近阶段新高/仍处低位'等定位判断)；\n"
        "- breadth：全市场宽度，up/down/flat=上涨/下跌/平盘家数，"
        "strong_up/strong_down=单日涨跌幅超过±9.8%的家数（近似涨跌停规模），"
        "avg_pct=全市场平均涨跌幅(%)，total_amount_yi=全市场成交额(亿元)，"
        "prev_amount_yi=上一交易日成交额(亿元)，"
        "avg5_amount_yi=前5个交易日平均成交额(亿元，量能趋势参考)；\n"
        "- top_movers：当日全市场涨幅榜/跌幅榜前10个股(已剔除新股上市首日"
        "等无涨跌幅限制的异常行)。可从中点名有代表性的领涨领跌个股并结合"
        "已知的公司主业/所属产业做定性归纳，但不要逐一罗列全部名单，"
        "也不要编造榜单之外的个股表现；\n"
        "- sector_boards：行业板块收盘快照，gainers/losers=涨幅前5/跌幅前5"
        "板块(pct_change=板块涨跌幅%，stock_count=板块成分股数，"
        "leader=领涨股及其涨跌幅)。为 null 表示当日板块数据缺失；\n"
        "- limit_up_ladder：涨停梯队近似统计(口径：单日涨幅≥9.8%，"
        "含大涨未封板，非严格涨停口径，表述时用'涨停及准涨停'类措辞)。"
        "count=当日家数，two_plus=其中连续第2天及以上的家数，"
        "max_streak=最高连续天数，max_streak_stocks=对应个股。"
        "为 null 表示当日数据缺失；\n"
        "- ai_hotsector：本平台的 AI 选股策略（每日选3板块×3股，T日收盘买入、"
        "T+1收盘卖出），today_sectors=今日新选板块，settled=昨日买入批次"
        "今日卖出的结算结果（win_count/total_count=上涨只数/总只数，"
        "day_return_pct=等权平均涨跌幅%）。字段为 null/空表示当日无数据。\n\n"
        f"{data_str}\n\n"
        "请基于以上数据写一篇当日 A 股市场复盘，markdown 格式，"
        "总字数 400~700 字，分以下五节（用 ## 二级标题）：\n"
        "## 大盘综述 —— 主要指数表现与阶段位置、量能变化"
        "（对比上一交易日与近5日均量的放量/缩量）\n"
        "## 板块聚焦 —— 领涨领跌板块及其领涨股，结合已知产业背景归纳"
        "当日主线与出血点；sector_boards 为 null 时如实写明板块数据缺失，"
        "可改用涨跌幅榜个股做侧面归纳\n"
        "## 市场情绪 —— 涨跌家数、大涨大跌家数反映的赚钱效应与亏钱效应；"
        "结合涨停梯队(count/two_plus/max_streak)评估情绪高度与持续性，"
        "注意按近似口径措辞\n"
        "## AI 策略表现 —— 点评 ai_hotsector 的结算战绩和今日新选板块；"
        "数据缺失就如实说明，不要硬编\n"
        "## 后市观察 —— 基于当日数据和已知产业/政策背景的定性观察，"
        "不构成投资建议，不给出具体买卖操作\n\n"
        "严格按以下 JSON 格式输出，不要有多余文字：\n"
        '{"title": "15-25字的复盘标题,概括当日行情特征,可含领涨板块名", '
        '"content_md": "markdown 正文"}'
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
