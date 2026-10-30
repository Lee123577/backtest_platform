"""
页面路由完整性
==============
每个 static 页面模板都要有路由挂着、sitemap 里的路径都要能访问。大段删改
main.py 时很容易连带删掉相邻的路由(之前删订阅页就差点把 /watchlist、
/dashboard、/my_board、/discuss、/legal、/s/{token} 一起删了),而现有测试
都是直接调函数、不经路由表,这种事故不会被别的用例发现。
"""
import pathlib

import pytest

import app.main as main

STATIC = pathlib.Path(main.__file__).resolve().parent / "static"

# 模板文件 → 期望挂载的页面路径
EXPECTED = {
    "index.html": "/",
    "paper_trading.html": "/paper_trading",
    "ai_hotsector.html": "/ai_hotsector",
    "daily_review.html": "/daily_review",
    "watchlist.html": "/watchlist",
    "dashboard.html": "/dashboard",
    "my_board.html": "/my_board",
    "discuss.html": "/discuss",
    "legal.html": "/legal",
    "share.html": "/s/{token}",
    "stock_report.html": "/stock/{code}",
    "strategy_index.html": "/strategy",
    "strategy.html": "/strategy/{strategy_id}",
    "tasks.html": "/admin/tasks",
}


def _get_paths():
    return {r.path for r in main.app.routes if "GET" in getattr(r, "methods", set())}


def test_每个页面模板都登记了期望路由():
    pages = {p.name for p in STATIC.glob("*.html")}
    assert pages == set(EXPECTED), f"模板与登记表不一致: {pages ^ set(EXPECTED)}"


@pytest.mark.parametrize("tpl,path", sorted(EXPECTED.items()))
def test_页面路由存在(tpl, path):
    assert path in _get_paths(), f"{tpl} 没有路由 {path}"


def test_sitemap_里的路径都有路由():
    paths = _get_paths()
    for path, *_ in main._SITEMAP_PAGES:
        assert path in paths, path
