"""
个股 AI 分析报告 API
=====================

GET  /api/stock_report/{code}          — 读报告(公开,不触发生成)
POST /api/stock_report/{code}/generate — 生成报告(三道闸,见 service.py)

读写分离是这个模块的成本底线:GET 永远不生成。爬虫顺着 /stock/{code} 爬
5000 个 URL 也只是 5000 次查库,一分钱不花;想生成必须显式 POST,而 POST
要过 IP 限流 + 全站日额度。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..data.data_loader import normalize_code
from ..csrf import reject_cross_site
from ..visit_log import _client_ip
from . import service
from .runner import generate_once

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stock_report", tags=["stock_report"])


def _norm(code: str) -> str:
    try:
        return normalize_code(code)
    except ValueError:
        raise HTTPException(400, "股票代码格式不合法")


@router.get("/{code}")
def get_report(code: str):
    code = _norm(code)
    row = service.get_report(code)
    if not row:
        # 404 而不是空对象:前端据此显示"还没有分析报告 + 生成按钮",
        # 页面直出那边也据此决定要不要出 noindex
        raise HTTPException(404, "这只股票还没有 AI 分析报告")
    return service.report_payload(row)


@router.post("/{code}/generate")
async def generate_report(code: str, request: Request):
    code = _norm(code)
    ip = _client_ip(request)

    # 写接口的老规矩:带 Origin/Referer 的浏览器请求必须同源。
    # 生成是花钱的动作,不能让别的站点用一张图片/表单把它触发起来。
    reject_cross_site(request)

    try:
        service.check_can_generate(ip)
    except service.RateLimited as e:
        raise HTTPException(429, str(e))
    except service.QuotaExceeded as e:
        raise HTTPException(429, str(e))

    result = await generate_once(code)
    if result.status == "skipped":
        # 已有当日报告是好事(直接读就行);数据不足则是这只票根本出不了报告
        row = service.get_report(code)
        if row:
            return service.report_payload(row)
        raise HTTPException(422, result.error_msg or "这只股票暂时无法生成分析报告")
    if result.status == "failed":
        raise HTTPException(502, "AI 分析生成失败，请稍后再试")

    row = service.get_report(code, result.report_date)
    if not row:
        raise HTTPException(500, "报告已生成但读取失败")
    return service.report_payload(row)
