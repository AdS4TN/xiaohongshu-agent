from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.github_trending import parse_trending_html


SAMPLE_HTML = """
<html>
  <body>
    <article class="Box-row">
      <h2>
        <a href="/owner/repo">
          owner / repo
        </a>
      </h2>
      <p>A useful AI project for testing the Trending parser locally.</p>
      <span itemprop="programmingLanguage">Python</span>
      <a href="/owner/repo/stargazers">1,234</a>
      <a href="/owner/repo/forks">56</a>
      <span>78 stars today</span>
    </article>
  </body>
</html>
"""


def main() -> None:
    items = parse_trending_html(
        SAMPLE_HTML,
        snapshot_time=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    first = items[0]
    assert first.source == "github_trending_daily"
    assert first.source_item_id == "owner/repo"
    assert first.metrics["rank"] == 1
    assert first.metrics["stars"] == 1234
    assert first.metrics["forks"] == 56
    assert first.metrics["stars_today"] == 78
    assert first.metrics["language"] == "Python"
    print(f"parsed={len(items)} source_item_id={first.source_item_id} metrics={first.metrics}")


if __name__ == "__main__":
    main()
