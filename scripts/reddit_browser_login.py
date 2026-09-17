from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.services.community_signal_expansion import _reddit_browser_channel, _reddit_browser_profile_dir


async def main() -> None:
    profile_dir = _reddit_browser_profile_dir()
    if profile_dir is None:
        raise SystemExit("未配置 REDDIT_BROWSER_PROFILE_DIR")
    profile_dir.mkdir(parents=True, exist_ok=True)

    channel = _reddit_browser_channel()
    settings = get_settings()

    from playwright.async_api import async_playwright

    launch_kwargs: dict[str, object] = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled", "--start-maximized"],
    }
    if channel:
        launch_kwargs["channel"] = channel

    print("=== Reddit 浏览器登录态初始化 ===")
    print(f"Profile 目录: {profile_dir}")
    print(f"Browser channel: {channel or 'chromium'}")
    print(f"应用时区: {settings.timezone}")
    print("即将打开 Reddit 登录页。请在浏览器里完成登录，然后回到终端按回车保存并退出。")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(**launch_kwargs)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.reddit.com/login/", wait_until="domcontentloaded", timeout=60_000)
            await page.bring_to_front()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, input, "登录完成后按回车退出...")
        finally:
            await context.close()

    print("Reddit 登录态已写入专用 profile，后续采集器会直接复用。")


if __name__ == "__main__":
    asyncio.run(main())
