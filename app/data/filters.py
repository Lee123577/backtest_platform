"""
A 股股票过滤器（共享辅助函数）
=============================

把分散在 market_data / paper_trading.runner 等地方的"判断股票是否符合条件"
逻辑收拢到一处，确保各回测/选股入口使用同一套规则。

主要 API：
  - is_st_name(name)               —— 名称前缀法识别 ST/退市
  - is_allowed_board(code, boards) —— 板块准入判断
  - board_of(code)                 —— 反查股票所属板块（用于统计/UI）
"""
from __future__ import annotations

from typing import Iterable, Tuple


# 沪市主板 600/601/603/605；深市主板 000/001/002/003
_MAIN_PREFIXES = ("600", "601", "603", "605", "000", "001", "002", "003")
_GEM_PREFIXES  = ("300", "301")
_STAR_PREFIXES = ("688", "689")
# 北交所:老段 4xx/8xx + 2024 年起的新段 920xxx
_BJ_PREFIXES   = ("4", "8", "920")


def is_st_name(name: str) -> bool:
    """名称前缀法识别 ST / 退市预警类股票。"""
    if not name:
        return False
    n = name.upper().replace(" ", "")
    if "退" in name:           # 含"退"的均视为退市相关
        return True
    return n.startswith(("ST", "*ST", "SST", "S*ST"))


def board_of(code: str) -> str:
    """
    返回 code 所属板块：'main' / 'gem' / 'star' / 'bj' / 'unknown'。
    """
    if not code:
        return "unknown"
    if code.startswith(_MAIN_PREFIXES):
        return "main"
    if code.startswith(_GEM_PREFIXES):
        return "gem"
    if code.startswith(_STAR_PREFIXES):
        return "star"
    if code.startswith(_BJ_PREFIXES):
        return "bj"
    return "unknown"


def is_allowed_board(code: str, allow_boards: Iterable[str]) -> bool:
    """
    allow_boards 形如 ('main',) 或 ['main','gem']。返回 code 是否在其中。
    未知板块（如新代码段）一律拒绝，避免误买入。
    """
    board = board_of(code)
    return board != "unknown" and board in allow_boards
