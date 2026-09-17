from app.adapters.github import GitHubAdapter
from app.adapters.github_trending import GitHubTrendingDailyAdapter
from app.adapters.arxiv import ArxivAdapter
from app.adapters.huggingface import HFDailyPapersAdapter, HFModelsAdapter, HFSpacesAdapter
from app.adapters.hackernews import HackerNewsAdapter

__all__ = [
    "GitHubAdapter",
    "GitHubTrendingDailyAdapter",
    "ArxivAdapter",
    "HFDailyPapersAdapter",
    "HFModelsAdapter",
    "HFSpacesAdapter",
    "HackerNewsAdapter",
]
