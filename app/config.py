from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class Settings(BaseSettings):
    """应用运行配置，优先从 .env 读取。"""

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    timezone: str = "Asia/Shanghai"

    database_url: str = "sqlite:///data/radar.sqlite"

    github_token: str | None = None
    hf_token: str | None = None

    llm_provider: str = "openai"
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.5,gpt-5.4"
    max_llm_candidates_per_run: int = 30

    research_agent_base_url: str = "http://127.0.0.1:40444/api/grok/v1/chat/completions"
    research_agent_model: str = "grok-4.20-multi-agent-xhigh"
    research_agent_timeout_seconds: float = 180.0

    github_info_search_base_url: str = "http://127.0.0.1:40444/api/grok/v1"
    github_info_search_model: str = "grok-4.20-multi-agent-xhigh"
    github_info_search_timeout_seconds: float = 360.0
    github_info_review_base_url: str = "http://47.250.164.154:8317/v1"
    github_info_review_api_key: str | None = None
    github_info_review_model: str = "gpt-5.5,gpt-5.4"
    github_info_summary_base_url: str = "http://47.250.164.154:8317/v1"
    github_info_summary_api_key: str | None = None
    github_info_summary_model: str = "gpt-5.5,gpt-5.4"
    github_info_writer_timeout_seconds: float = 240.0
    mcp_git_ingest_local_path: str | None = None
    enable_github_info_community_signals: bool = True
    community_signal_timeout_seconds: float = 30.0
    community_signal_max_results_per_platform: int = 6
    linux_do_cookie_header: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str | None = None
    reddit_browser_profile_dir: str | None = "data/browser_profiles/reddit"
    reddit_browser_channel: str = "chrome"
    reddit_browser_headless: bool = False
    reddit_devvit_shared_token: str | None = None
    reddit_devvit_watchlist_limit: int = 50

    daily_report_max_hotspots: int = 12
    daily_report_lookback_hours: int = 48
    daily_report_timeout_seconds: float = 240.0

    # A/B 层博客文字撰写接口。默认复用 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL。
    blog_analysis_base_url: str | None = None
    blog_analysis_model: str | None = None
    blog_analysis_timeout_seconds: float = 120.0
    enable_blog_analysis_llm: bool = True

    # 个人博客 AI/科技热点 JSON 导出。
    # PUBLIC_EXPORT_DIR 是本项目内的标准导出目录；
    # BLOG_EXPORT_DIR 可选，设置后会把同一份 JSON 同步到博客项目目录。
    public_export_dir: str = "public_exports/ai-hotspots"
    blog_export_dir: str | None = None
    blog_detail_base_path: str = "/ai-hotspots"
    daily_card_limit: int = 8
    daily_export_lookback_hours: int = 48

    enable_scheduler: bool = True
    daily_run_time: str = "08:00"
    evening_run_time: str = "18:00"

    request_timeout_seconds: float = 15.0
    max_items_per_source: int = 100

    # News ???????AI Radar ?? GitHub Daily ??????????
    # ?????? SSE ???????
    news_dashboard_webhook_url: str | None = "http://127.0.0.1:40444/api/news/github-trending/notify"
    news_dashboard_webhook_token: str | None = None
    news_dashboard_webhook_timeout_seconds: float = 5.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def sqlite_path(self) -> Path | None:
        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url.removeprefix("sqlite:///"))
        return None


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    sqlite_path = settings.sqlite_path
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
