from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db
from app.main import app
from app.models import DailyReportModel, HotspotModel
from app.services.blog_export import build_export_payload
from fastapi.testclient import TestClient


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def ok(message: str) -> None:
    print(f"OK: {message}")


def main() -> None:
    prd_path = ROOT / "docs" / "AI_HOTSPOTS_SITE_PRD.md"
    writer_agent_path = ROOT / "docs" / "AGENT_WRITER.md"
    if not prd_path.exists():
        fail("缺少 PRD 文档 docs/AI_HOTSPOTS_SITE_PRD.md。")
    if not writer_agent_path.exists():
        fail("缺少写作智能体说明 docs/AGENT_WRITER.md。")
    forbidden_doc_terms = [
        "普通人/从业者",
        "值不值得",
        "继续追踪",
        "要不要关注",
        "最值得看",
        "能引发点击",
        "有判断",
        "这件事对我有什么意义",
    ]
    prd_text = prd_path.read_text(encoding="utf-8")
    for term in forbidden_doc_terms:
        if term in prd_text:
            fail(f"PRD 仍包含非客观口径词：{term}")
    ok("PRD 与写作智能体文档存在，且 PRD 未命中主观口径禁词。")

    init_db()
    db = SessionLocal()
    try:
        hotspot_count = db.query(HotspotModel).count()
        if hotspot_count <= 0:
            fail("数据库中没有热点，请先运行采集。")
        ok(f"热点数量：{hotspot_count}")

        payload = build_export_payload(db, limit=3)
        cards = payload.get("cards") or []
        if not cards:
            fail("首页卡片为空。")
        ok(f"首页卡片数量：{len(cards)}")

        items = payload.get("items") or []
        first = items[0] if items else None
        if not first:
            fail("没有可验收的热点详情。")

        detail = first.get("detail") or {}
        article_title = (detail.get("article_title") or "").strip()
        article_content = (detail.get("article_content") or "").strip()
        if not article_title:
            fail("热点详情缺少 article_title。")
        if len(article_content) < 80:
            fail("热点详情 article_content 过短，疑似未生成文章。")
        forbidden_sections = ["关键事实", "为什么重要", "影响面", "客观分析"]
        if any(section in article_content for section in forbidden_sections):
            fail("热点详情正文仍包含报告式章节标题。")
        ok("单热点详情已按短文结构导出。")

        source_count = len(detail.get("related_sources") or [])
        if source_count <= 0:
            fail("热点详情缺少相关来源。")
        ok(f"相关来源数量：{source_count}")

        client = TestClient(app)
        raw_page = client.get("/raw-sources")
        if raw_page.status_code != 200:
            fail(f"原始数据页面不可访问：HTTP {raw_page.status_code}")
        if "POST /api/raw-sources/query" not in raw_page.text:
            fail("原始数据页面缺少前端 POST 接口说明。")
        if "payload 结构化视图" not in raw_page.text:
            fail("原始数据页面缺少 payload 结构化视图。")
        if "classifyPayloadKey" not in raw_page.text:
            fail("原始数据页面缺少 payload 字段分组逻辑。")

        raw_api = client.post("/api/raw-sources/query", json={"limit": 1, "offset": 0})
        if raw_api.status_code != 200:
            fail(f"原始数据 API 不可访问：HTTP {raw_api.status_code}")
        raw_payload = raw_api.json()
        if raw_payload.get("status") != "success":
            fail("原始数据 API 未返回 success。")
        raw_items = raw_payload.get("items") or []
        if not raw_items:
            fail("原始数据 API 未返回 source_items。")
        first_raw = raw_items[0]
        if "raw_payload_json" not in first_raw:
            fail("原始数据 API 缺少 raw_payload_json 原始字符串。")
        if "raw_payload" not in first_raw:
            fail("原始数据 API 缺少 raw_payload 解析对象。")
        if "total" not in raw_payload or "has_more" not in raw_payload:
            fail("原始数据 API 缺少分页字段 total/has_more。")
        ok("原始数据页面与 POST API 可用。")

        latest_daily = db.query(DailyReportModel).order_by(DailyReportModel.created_at.desc()).first()
        if latest_daily is None:
            ok("C 层/日报缓存尚未生成；接口存在但未触发。")
        elif latest_daily.status not in {"success", "running", "failed"}:
            fail(f"C 层/日报状态异常：{latest_daily.status}")
        else:
            ok(f"C 层/日报最近状态：{latest_daily.status}")

        print(
            json.dumps(
                {
                    "status": "passed",
                    "hotspot_count": hotspot_count,
                    "sample_title": first.get("title"),
                    "sample_article_chars": len(article_content),
                    "sample_sources": source_count,
                    "daily_report_status": latest_daily.status if latest_daily else "not_generated",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
