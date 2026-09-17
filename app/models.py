from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SourceItemModel(Base):
    __tablename__ = "source_items"
    __table_args__ = (
        UniqueConstraint("source", "source_item_id", name="uq_source_item"),
        UniqueConstraint("content_hash", name="uq_source_item_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_item_id: Mapped[str] = mapped_column(String(512), index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    hotspots: Mapped[list["HotspotSourceModel"]] = relationship(back_populates="source_item")


class SourceRunModel(Base):
    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_fetched: Mapped[int] = mapped_column(Integer, default=0)
    items_inserted: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class GitHubRepoSnapshotModel(Base):
    __tablename__ = "github_repo_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(String(512), index=True)
    stars: Mapped[int] = mapped_column(Integer, default=0)
    forks: Mapped[int] = mapped_column(Integer, default=0)
    open_issues: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snapshot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HotspotModel(Base):
    __tablename__ = "hotspots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    why_it_matters: Mapped[str | None] = mapped_column(Text, nullable=True)
    xiaohongshu_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(String(64), default="tech_news", index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    risk: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sources: Mapped[list["HotspotSourceModel"]] = relationship(back_populates="hotspot", cascade="all, delete-orphan")
    selections: Mapped[list["UserSelectionModel"]] = relationship(back_populates="hotspot", cascade="all, delete-orphan")
    research_reports: Mapped[list["ResearchReportModel"]] = relationship(back_populates="hotspot", cascade="all, delete-orphan")
    blog_analyses: Mapped[list["BlogAnalysisModel"]] = relationship(back_populates="hotspot", cascade="all, delete-orphan")


class HotspotSourceModel(Base):
    __tablename__ = "hotspot_sources"
    __table_args__ = (UniqueConstraint("hotspot_id", "source_item_id", name="uq_hotspot_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotspot_id: Mapped[int] = mapped_column(ForeignKey("hotspots.id"), index=True)
    source_item_id: Mapped[int] = mapped_column(ForeignKey("source_items.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    hotspot: Mapped[HotspotModel] = relationship(back_populates="sources")
    source_item: Mapped[SourceItemModel] = relationship(back_populates="hotspots")


class UserSelectionModel(Base):
    __tablename__ = "user_selections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotspot_id: Mapped[int] = mapped_column(ForeignKey("hotspots.id"), index=True)
    selection_type: Mapped[str] = mapped_column(String(32), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    hotspot: Mapped[HotspotModel] = relationship(back_populates="selections")


class ResearchReportModel(Base):
    __tablename__ = "research_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotspot_id: Mapped[int] = mapped_column(ForeignKey("hotspots.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_json: Mapped[str] = mapped_column(Text, default="{}")
    report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    hotspot: Mapped[HotspotModel] = relationship(back_populates="research_reports")


class DailyReportModel(Base):
    """AI/科技今日报告缓存。

    流水线：API/RSS 采集热点 → research agent 联网补充资料 → Codex/OpenAI 兼容接口撰写整篇报告。
    """

    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("report_date", "report_type", name="uq_daily_report_date_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[str] = mapped_column(String(16), index=True)
    report_type: Mapped[str] = mapped_column(String(64), default="ai_tech_daily", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    model: Mapped[str] = mapped_column(String(256))
    prompt_json: Mapped[str] = mapped_column(Text, default="{}")
    report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_snapshot_json: Mapped[str] = mapped_column(Text, default="[]")
    research_pack_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BlogAnalysisModel(Base):
    """博客 A/B 层结构化分析缓存。

    A/B 层由 Codex/OpenAI 兼容接口预生成；页面访问只读取该缓存，不实时调用模型。
    """

    __tablename__ = "blog_analyses"
    __table_args__ = (UniqueConstraint("hotspot_id", "analysis_version", name="uq_blog_analysis_hotspot_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hotspot_id: Mapped[int] = mapped_column(ForeignKey("hotspots.id"), index=True)
    analysis_version: Mapped[str] = mapped_column(String(64), default="blog_ab_v1", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_json: Mapped[str] = mapped_column(Text, default="{}")
    analysis_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    hotspot: Mapped[HotspotModel] = relationship(back_populates="blog_analyses")
