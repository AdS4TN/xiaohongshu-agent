from __future__ import annotations

import json
import logging
import os
import re
import asyncio
import importlib.util
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
import sys
import types
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.model_utils import format_model_candidates, parse_model_candidates
from app.models import SourceItemModel
from app.services.community_signal_expansion import expand_community_signals

logger = logging.getLogger(__name__)

GITHUB_INFO_EXPANSION_VERSION = "github_info_expansion_v1"
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HTML_META_DESCRIPTION_RE = re.compile(
    r"<meta[^>]+(?:name|property)=[\"'](?:description|og:description|twitter:description)[\"'][^>]+content=[\"'](.*?)[\"'][^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_MAX_SOURCE_CONTENT_FETCHES = 30
_SOURCE_CONTENT_TIMEOUT_SECONDS = 12.0
_SOURCE_SNIPPET_CHARS = 4000
_REPO_INGEST_TIMEOUT_SECONDS = 25.0
_REPO_INGEST_MAX_FILES = 80
_REPO_INGEST_MAX_EXCERPT_FILES = 10
_REPO_INGEST_EXCERPT_CHARS = 2500
_GPT_WRITER_THREAD_LOCK = threading.Lock()
_GPT_WRITER_LOCK_PATH = Path(tempfile.gettempdir()) / "ai_radar_github_info_gpt.lock"
_MCP_GIT_INGEST_COMMON_FILES = [
    "README.md",
    "README",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
    "Makefile",
    "docs/README.md",
    "examples/README.md",
    "example/README.md",
    "demo/README.md",
]


@dataclass(frozen=True)
class ChatEndpoint:
    """OpenAI 兼容 chat completions 端点配置。"""

    base_url: str
    model: str
    api_key: str | None
    timeout_seconds: float

    @property
    def chat_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    @property
    def headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def model_candidates(self) -> list[str]:
        return parse_model_candidates(self.model)


def _settings_endpoints() -> tuple[ChatEndpoint, ChatEndpoint, ChatEndpoint]:
    settings = get_settings()
    search = ChatEndpoint(
        base_url=settings.github_info_search_base_url,
        model=settings.github_info_search_model,
        api_key=None,
        timeout_seconds=settings.github_info_search_timeout_seconds,
    )
    review = ChatEndpoint(
        base_url=settings.github_info_review_base_url,
        model=settings.github_info_review_model,
        api_key=settings.github_info_review_api_key,
        timeout_seconds=settings.github_info_writer_timeout_seconds,
    )
    summary = ChatEndpoint(
        base_url=settings.github_info_summary_base_url,
        model=settings.github_info_summary_model,
        api_key=settings.github_info_summary_api_key,
        timeout_seconds=settings.github_info_writer_timeout_seconds,
    )
    return search, review, summary


def _safe_json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_chat_content(response: httpx.Response) -> str:
    """兼容普通 JSON、SSE data 行和少数纯文本代理响应。"""

    text = response.text.strip()
    try:
        data = response.json()
        return data["choices"][0]["message"]["content"] or ""
    except Exception:
        pass

    chunks: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choice = (data.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        message = choice.get("message") or {}
        content = delta.get("content") or message.get("content")
        if content:
            chunks.append(str(content))
    if chunks:
        return "".join(chunks)
    return text


def _parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出中提取 JSON object；失败时保留原文，避免丢证据。"""

    raw = (text or "").strip()
    if not raw:
        return {"_parse_error": "empty_response", "raw_text": ""}

    candidates = [raw]
    candidates.extend(match.group(1).strip() for match in _JSON_BLOCK_RE.finditer(raw))
    first = raw.find("{")
    last = raw.rfind("}")
    if 0 <= first < last:
        candidates.append(raw[first : last + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return {"_parse_error": "invalid_json", "raw_text": raw}


class _HeldGptWriterLock:
    def __init__(self, lock_file: Any, backend: Literal["windows", "posix"]) -> None:
        self.lock_file = lock_file
        self.backend = backend
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        try:
            if self.backend == "windows":
                import msvcrt

                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self.released = True
            self.lock_file.close()
            _GPT_WRITER_THREAD_LOCK.release()


def _acquire_gpt_writer_serial_lock() -> _HeldGptWriterLock:
    """跨线程/跨进程串行化 GPT 审核和摘要调用。

    GitHub 信息扩展允许 Grok 搜索并行，但 review/summary 共享同一个 GPT 写作
    OpenAI-compatible 端点。这里使用线程锁 + OS 文件锁，避免同时开着 8000/8010
    两个 uvicorn 进程时仍然并发打到 GPT。
    """

    _GPT_WRITER_THREAD_LOCK.acquire()
    lock_file = None
    try:
        _GPT_WRITER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not _GPT_WRITER_LOCK_PATH.exists():
            _GPT_WRITER_LOCK_PATH.write_bytes(b"\0")
        elif _GPT_WRITER_LOCK_PATH.stat().st_size == 0:
            try:
                _GPT_WRITER_LOCK_PATH.write_bytes(b"\0")
            except OSError:
                # 其他进程可能正持有 Windows 强制锁；文件内容不重要，后续直接竞争锁即可。
                pass
        lock_file = _GPT_WRITER_LOCK_PATH.open("r+b")
        lock_file.seek(0)

        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    return _HeldGptWriterLock(lock_file, "windows")
                except OSError:
                    time.sleep(0.25)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return _HeldGptWriterLock(lock_file, "posix")
                except OSError:
                    time.sleep(0.25)
    except Exception:
        if lock_file is not None:
            lock_file.close()
        _GPT_WRITER_THREAD_LOCK.release()
        raise


async def _chat(
    endpoint: ChatEndpoint,
    messages: list[dict[str, str]],
    *,
    response_format: dict[str, str] | None = None,
) -> tuple[str, str]:
    max_attempts = 5
    candidates = endpoint.model_candidates or [endpoint.model]
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=endpoint.timeout_seconds) as client:
        for index, model_name in enumerate(candidates):
            payload: dict[str, Any] = {
                "model": model_name,
                "messages": messages,
                "temperature": 0.1,
            }
            if response_format is not None:
                payload["response_format"] = response_format

            for attempt in range(max_attempts):
                try:
                    response = await client.post(endpoint.chat_url, json=payload, headers=endpoint.headers)
                    response.raise_for_status()
                    return _extract_chat_content(response), model_name
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    # 有些 OpenAI-compatible 代理不支持 response_format。
                    # 提示词本身已经要求严格 JSON；这里降级重试一次，避免整条流水线被兼容性细节卡死。
                    if response_format is not None and exc.response.status_code in {400, 422}:
                        error_text = exc.response.text.lower()
                        if "response_format" in error_text or "json_object" in error_text:
                            downgraded_payload = dict(payload)
                            downgraded_payload.pop("response_format", None)
                            try:
                                response = await client.post(endpoint.chat_url, json=downgraded_payload, headers=endpoint.headers)
                                response.raise_for_status()
                                return _extract_chat_content(response), model_name
                            except Exception as inner_exc:
                                last_error = inner_exc if isinstance(inner_exc, Exception) else exc
                    if exc.response.status_code in {429, 500, 502, 503, 504} and attempt < max_attempts - 1:
                        await asyncio.sleep(min(20.0, 2.0 * (attempt + 1)))
                        continue
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(min(20.0, 2.0 * (attempt + 1)))
                        continue
                    break

            if index < len(candidates) - 1:
                logger.warning("模型 %s 调用失败，回退到 %s：%s", model_name, candidates[index + 1], last_error)

    if last_error is not None:
        raise last_error
    raise RuntimeError("chat completion failed after retries")


async def _chat_serialized_gpt(
    endpoint: ChatEndpoint,
    messages: list[dict[str, str]],
    *,
    stage: str,
    response_format: dict[str, str] | None = None,
) -> tuple[str, str]:
    logger.info("等待 GPT 串行锁 stage=%s models=%s", stage, format_model_candidates(endpoint.model))
    held_lock = await asyncio.to_thread(_acquire_gpt_writer_serial_lock)
    used_model: str | None = None
    try:
        logger.info("开始 GPT 串行调用 stage=%s models=%s", stage, format_model_candidates(endpoint.model))
        text, used_model = await _chat(endpoint, messages, response_format=response_format)
        return text, used_model
    finally:
        await asyncio.to_thread(held_lock.release)
        logger.info("释放 GPT 串行锁 stage=%s used_model=%s", stage, used_model or format_model_candidates(endpoint.model))


async def _chat_json_object(
    endpoint: ChatEndpoint,
    messages: list[dict[str, str]],
    *,
    stage: str,
    serialize_gpt: bool = False,
    max_parse_attempts: int = 2,
) -> tuple[str, dict[str, Any], str]:
    """调用模型并解析 JSON；解析失败时追加修正提示再试一次。

    - Grok 搜索阶段 serialize_gpt=False，可由多个项目并行执行。
    - GPT 审核/摘要阶段 serialize_gpt=True，跨线程/跨进程串行执行。
    """

    current_messages = list(messages)
    attempts: list[dict[str, Any]] = []
    last_text = ""
    last_parsed: dict[str, Any] = {"_parse_error": "not_started"}
    last_used_model = endpoint.model_candidates[0] if endpoint.model_candidates else endpoint.model

    for attempt in range(1, max_parse_attempts + 1):
        if serialize_gpt:
            text, used_model = await _chat_serialized_gpt(
                endpoint,
                current_messages,
                stage=f"{stage}#{attempt}",
                response_format={"type": "json_object"},
            )
        else:
            text, used_model = await _chat(endpoint, current_messages, response_format={"type": "json_object"})

        parsed = _parse_json_object(text)
        parse_error = parsed.get("_parse_error")
        attempts.append(
            {
                "attempt": attempt,
                "model": used_model,
                "parse_error": parse_error,
                "raw_preview": text[:800],
            }
        )
        last_text = text
        last_parsed = parsed
        last_used_model = used_model
        if not parse_error:
            if attempt > 1:
                parsed["_parse_attempts"] = attempts
            return text, parsed, used_model

        logger.warning("模型 JSON 解析失败 stage=%s attempt=%s error=%s", stage, attempt, parse_error)
        current_messages = [
            *messages,
            {"role": "assistant", "content": text[:6000]},
            {
                "role": "user",
                "content": (
                    "上一次输出无法解析成严格 JSON object。请重新输出：只允许一个 JSON object，"
                    "不要 Markdown，不要代码块，不要解释性文字，不要在 JSON 前后添加任何字符。"
                ),
            },
        ]

    last_parsed["_parse_attempts"] = attempts
    return last_text, last_parsed, last_used_model


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _github_owner_repo(url: str | None) -> tuple[str, str] | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1].removesuffix(".git")


def _html_to_text(html: str, *, max_chars: int = _SOURCE_SNIPPET_CHARS) -> str:
    text = _HTML_SCRIPT_STYLE_RE.sub(" ", html)
    text = _HTML_TAG_RE.sub(" ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _extract_html_title(html: str) -> str | None:
    match = _HTML_TITLE_RE.search(html)
    if not match:
        return None
    return _html_to_text(match.group(1), max_chars=300)


def _extract_meta_description(html: str) -> str | None:
    match = _HTML_META_DESCRIPTION_RE.search(html)
    if not match:
        return None
    return unescape(re.sub(r"\s+", " ", match.group(1)).strip())[:800]


async def _fetch_one_source_content(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    source: dict[str, Any],
) -> dict[str, Any]:
    source_id = str(source.get("id") or "")
    url = str(source.get("url") or "").strip()
    if not _is_http_url(url):
        return {
            "source_id": source_id,
            "url": url,
            "fetched": False,
            "status_code": None,
            "final_url": None,
            "content_type": None,
            "title": None,
            "meta_description": None,
            "text_excerpt": "",
            "error": "invalid_or_missing_http_url",
        }

    async with semaphore:
        try:
            response = await client.get(url)
            content_type = response.headers.get("content-type") or ""
            text = response.text if response.content else ""
            title = _extract_html_title(text) if "html" in content_type.lower() or "<html" in text[:500].lower() else None
            meta_description = _extract_meta_description(text) if text else None
            text_excerpt = _html_to_text(text) if text else ""
            return {
                "source_id": source_id,
                "url": url,
                "fetched": 200 <= response.status_code < 400 and bool(text_excerpt),
                "status_code": response.status_code,
                "final_url": str(response.url),
                "content_type": content_type,
                "title": title,
                "meta_description": meta_description,
                "text_excerpt": text_excerpt,
                "error": None,
            }
        except Exception as exc:
            return {
                "source_id": source_id,
                "url": url,
                "fetched": False,
                "status_code": None,
                "final_url": None,
                "content_type": None,
                "title": None,
                "meta_description": None,
                "text_excerpt": "",
                "error": type(exc).__name__,
            }


async def _annotate_source_content_evidence(search_pack: dict[str, Any]) -> dict[str, Any]:
    """抓取搜索 AI 返回的网页内容片段，供审核 AI 判断“是否相关、是否能支撑事实”。

    这里不把“能打开”当成通过，只把 title/meta/正文片段作为证据快照交给审核 AI。
    真正的相关性判断和事实支撑判断仍由审核 AI 完成。
    """

    sources = search_pack.get("sources")
    if not isinstance(sources, list):
        search_pack["source_content_evidence"] = {
            "fetched_at": _utc_now_iso(),
            "fetched_count": 0,
            "items": [],
            "notes": "搜索包没有 sources 数组，无法抓取网页内容片段。",
        }
        return search_pack

    dict_sources = [source for source in sources if isinstance(source, dict)]
    targets = dict_sources[:_MAX_SOURCE_CONTENT_FETCHES]
    semaphore = asyncio.Semaphore(6)
    headers = {
        "User-Agent": "Mozilla/5.0 GitHubInfoExpansionBot/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(
        timeout=_SOURCE_CONTENT_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
    ) as client:
        items = await asyncio.gather(*(_fetch_one_source_content(client, semaphore, source) for source in targets))

    search_pack["source_content_evidence"] = {
        "fetched_at": _utc_now_iso(),
        "fetched_count": len(items),
        "skipped_count": max(0, len(dict_sources) - len(targets)),
        "items": items,
        "notes": "fetched=true 只表示抓到了页面文本片段；审核 AI 必须检查片段是否与目标 GitHub 项目相关，以及是否足以支撑 claim。",
    }
    return search_pack


def _repo_ingest_headers() -> dict[str, str]:
    settings = get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "GitHubInfoExpansionBot/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if settings.github_token:
        headers["Authorization"] = f"Bearer {settings.github_token}"
    return headers


def _mcp_git_ingest_path() -> Path:
    settings = get_settings()
    if settings.mcp_git_ingest_local_path:
        return Path(settings.mcp_git_ingest_local_path)
    return Path.home() / "mcp-git-ingest-local"


def _load_local_mcp_git_ingest() -> Any | None:
    """加载用户本地的 mcp-git-ingest MCP 实现。

    这里不是启动 MCP server，而是复用其 tool 函数实现：
    - git_directory_structure
    - git_read_important_files

    本项目运行环境未安装 fastmcp，因此加载时注入一个最小 FastMCP stub，
    让工具函数可以被直接调用。
    """

    root = _mcp_git_ingest_path()
    main_py = root / "src" / "mcp_git_ingest" / "main.py"
    if not main_py.exists():
        return None

    module_name = "_local_mcp_git_ingest_main"
    if module_name in sys.modules:
        return sys.modules[module_name]

    fake_fastmcp = types.ModuleType("fastmcp")

    class FastMCPStub:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def tool(self):
            def decorator(fn):
                return fn

            return decorator

    fake_fastmcp.FastMCP = FastMCPStub
    original_fastmcp = sys.modules.get("fastmcp")
    sys.modules["fastmcp"] = fake_fastmcp
    try:
        spec = importlib.util.spec_from_file_location(module_name, main_py)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(module_name, None)
        return None
    finally:
        if original_fastmcp is not None:
            sys.modules["fastmcp"] = original_fastmcp
        else:
            sys.modules.pop("fastmcp", None)


def _repo_ingest_via_local_mcp_sync(repo_url: str) -> dict[str, Any]:
    module = _load_local_mcp_git_ingest()
    if module is None:
        return {"status": "skipped", "reason": "local_mcp_git_ingest_not_found", "local_path": str(_mcp_git_ingest_path())}

    tree = module.git_directory_structure(repo_url)
    if isinstance(tree, str) and tree.startswith("Error:"):
        return {"status": "failed", "tool": "mcp-git-ingest-local", "error_message": tree}

    file_contents = module.git_read_important_files(repo_url, _MCP_GIT_INGEST_COMMON_FILES)
    if not isinstance(file_contents, dict):
        file_contents = {}
    excerpts = []
    for path, content in file_contents.items():
        if not isinstance(content, str) or content.startswith("Error:") or not content.strip():
            continue
        excerpts.append(
            {
                "path": path,
                "excerpt": content[:_REPO_INGEST_EXCERPT_CHARS],
            }
        )

    return {
        "status": "success",
        "tool": "mcp-git-ingest-local",
        "local_path": str(_mcp_git_ingest_path()),
        "repo_url": repo_url,
        "directory_tree": tree,
        "file_excerpts": excerpts,
        "read_file_candidates": _MCP_GIT_INGEST_COMMON_FILES,
        "note": "通过用户本地 Mcp-git-ingest MCP 源码中的 git_directory_structure / git_read_important_files 工具函数读取仓库。",
    }


async def _repo_ingest_via_local_mcp(repo_url: str) -> dict[str, Any]:
    return await asyncio.to_thread(_repo_ingest_via_local_mcp_sync, repo_url)


def _is_interesting_repo_file(path: str) -> bool:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    if name in {
        "readme.md",
        "readme",
        "package.json",
        "pyproject.toml",
        "cargo.toml",
        "go.mod",
        "requirements.txt",
        "dockerfile",
        "compose.yaml",
        "docker-compose.yml",
        "makefile",
        "license",
    }:
        return True
    if lower.startswith(("docs/", "examples/", "demo/", "demos/", "samples/", "src/", "app/", "packages/")):
        return True
    return False


async def _github_repo_ingest(project: dict[str, Any]) -> dict[str, Any]:
    """优先使用本地 mcp-git-ingest 读取仓库，再用 GitHub API 兜底。"""

    owner_repo = _github_owner_repo(project.get("url"))
    if owner_repo is None:
        return {"status": "skipped", "reason": "not_a_github_repo_url", "repo_url": project.get("url")}

    owner, repo = owner_repo
    repo_url = f"https://github.com/{owner}/{repo}"
    local_mcp_result = await _repo_ingest_via_local_mcp(repo_url)
    if local_mcp_result.get("status") == "success":
        return local_mcp_result

    headers = _repo_ingest_headers()
    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    result: dict[str, Any] = {
        "status": "running",
        "tool": "github_api_repo_ingest_fallback",
        "requested_tool": "mcp-git-ingest",
        "local_mcp_result": local_mcp_result,
        "note": "本地 mcp-git-ingest 未成功运行，使用 GitHub API 做轻量仓库结构读取，供摘要 AI 判断项目用途。",
        "repo": f"{owner}/{repo}",
        "repo_url": project.get("url"),
    }

    try:
        async with httpx.AsyncClient(timeout=_REPO_INGEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers) as client:
            repo_response = await client.get(api_base)
            repo_response.raise_for_status()
            repo_json = repo_response.json()
            default_branch = repo_json.get("default_branch") or "main"
            result["metadata"] = {
                "full_name": repo_json.get("full_name"),
                "description": repo_json.get("description"),
                "homepage": repo_json.get("homepage"),
                "language": repo_json.get("language"),
                "topics": repo_json.get("topics"),
                "stars": repo_json.get("stargazers_count"),
                "forks": repo_json.get("forks_count"),
                "default_branch": default_branch,
                "license": repo_json.get("license"),
            }

            tree_response = await client.get(f"{api_base}/git/trees/{default_branch}?recursive=1")
            tree_response.raise_for_status()
            tree_json = tree_response.json()
            tree_items = tree_json.get("tree") if isinstance(tree_json.get("tree"), list) else []
            blob_paths = [
                item.get("path")
                for item in tree_items
                if isinstance(item, dict) and item.get("type") == "blob" and isinstance(item.get("path"), str)
            ]
            tree_paths = [
                item.get("path")
                for item in tree_items
                if isinstance(item, dict) and item.get("type") == "tree" and isinstance(item.get("path"), str)
            ]
            top_level_dirs = sorted({path.split("/", 1)[0] for path in tree_paths if "/" not in path or path.split("/", 1)[0]})
            interesting_files = [path for path in blob_paths if _is_interesting_repo_file(path)][:_REPO_INGEST_MAX_FILES]
            result["structure"] = {
                "tree_truncated": bool(tree_json.get("truncated")),
                "file_count": len(blob_paths),
                "top_level_dirs": top_level_dirs[:40],
                "interesting_files": interesting_files,
            }

            excerpts = []
            for path in interesting_files[:_REPO_INGEST_MAX_EXCERPT_FILES]:
                raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/{path}"
                try:
                    file_response = await client.get(raw_url)
                    if file_response.status_code >= 400:
                        continue
                    text = file_response.text
                    if not text.strip():
                        continue
                    excerpts.append(
                        {
                            "path": path,
                            "raw_url": raw_url,
                            "excerpt": text[:_REPO_INGEST_EXCERPT_CHARS],
                        }
                    )
                except Exception:
                    continue
            result["file_excerpts"] = excerpts
            result["status"] = "success"
            return result
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = type(exc).__name__
        result["error_message"] = str(exc)[:1000]
        return result


def build_project_context(item: SourceItemModel) -> dict[str, Any]:
    """构造单个 GitHub 项目的上下文，尽量只给搜索/审核/摘要需要的信息。"""

    metrics = _safe_json_loads(item.metrics_json, {})
    if not isinstance(metrics, dict):
        metrics = {}
    raw_payload = _safe_json_loads(item.raw_payload_json, {})
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    readme_text = raw_payload.get("readme_text") or raw_payload.get("readme_excerpt") or ""
    repo_payload = raw_payload.get("github_api_repo") if isinstance(raw_payload.get("github_api_repo"), dict) else {}
    source_urls = raw_payload.get("evidence_source_urls") if isinstance(raw_payload.get("evidence_source_urls"), list) else []
    return {
        "source_item_id": item.source_item_id,
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "summary": item.summary,
        "author": item.author,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "metrics": {
            "rank": metrics.get("rank"),
            "stars_today": metrics.get("stars_today"),
            "stars": metrics.get("stars"),
            "forks": metrics.get("forks"),
            "language": metrics.get("primary_language_api") or metrics.get("language"),
            "topics": metrics.get("topics"),
            "repo_created_at": metrics.get("repo_created_at"),
            "repo_pushed_at": metrics.get("repo_pushed_at"),
            "latest_release_name": metrics.get("latest_release_name"),
            "latest_release_at": metrics.get("latest_release_at"),
            "license": metrics.get("license"),
            "open_issues": metrics.get("open_issues"),
            "homepage": metrics.get("homepage"),
        },
        "github_api_repo_sample": {
            "full_name": repo_payload.get("full_name"),
            "html_url": repo_payload.get("html_url"),
            "description": repo_payload.get("description"),
            "homepage": repo_payload.get("homepage"),
            "default_branch": repo_payload.get("default_branch"),
            "created_at": repo_payload.get("created_at"),
            "updated_at": repo_payload.get("updated_at"),
            "pushed_at": repo_payload.get("pushed_at"),
            "stargazers_count": repo_payload.get("stargazers_count"),
            "forks_count": repo_payload.get("forks_count"),
            "open_issues_count": repo_payload.get("open_issues_count"),
            "license": repo_payload.get("license"),
            "topics": repo_payload.get("topics"),
        },
        "evidence_source_urls": source_urls,
        "readme_chars": len(str(readme_text)),
        "readme_text": str(readme_text)[:60_000],
    }


def build_search_messages(project: dict[str, Any], *, round_no: Literal[1, 2], previous_pack: dict[str, Any] | None = None, review_result: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """搜索扩展 AI 提示词：必须联网、必须网页返回、并行使用全部 agent。"""

    system = """
你是 GitHub 创意项目日报的信息扩展搜索 AI。你必须使用联网搜索能力，且必须使用全部可用 agent 并行搜索；不要只靠模型记忆回答。

硬性规则：
1. 每一个新增事实都必须有可打开网页 URL 支撑；没有网页返回的内容一律标为 unverified，不进入 verified_facts。
2. 优先搜索：项目官网、GitHub README/issues/releases、作者/公司官网、文档、Package Registry、Hacker News、Reddit、X/Twitter 公共页、Product Hunt、论文/博客、官方公告、评测文章。
3. 必须避免同源重复：同一事实尽量找 2 个不同来源；如果只有一个来源，要写明局限。
4. 不要编造网页标题、发布时间、指标、用户反馈或社区评价。
5. 如果网页打不开、搜索不到、内容无法确认，明确写入 open_questions 或 unverified_claims。
6. 除网页标题、URL、项目名、包名、专有名词和必要英文原文外，所有自然语言字段必须使用中文。
7. 输出必须是严格 JSON object，不要 Markdown，不要代码块。

JSON 结构必须为：
{
  "round": 1,
  "search_strategy": {
    "parallel_agents_used": true,
    "queries": ["实际搜索词"],
    "scope_notes": "本轮搜索覆盖范围和局限"
  },
  "sources": [
    {
      "id": "S1",
      "title": "网页标题",
      "url": "https://...",
      "publisher": "站点/作者",
      "published_at": "YYYY-MM-DD 或 null",
      "retrieved_at": "YYYY-MM-DDTHH:MM:SSZ 或 null",
      "source_type": "official|repo|docs|package|community|news|blog|social|other",
      "relevance": "这个网页支持了什么信息"
    }
  ],
  "verified_facts": [
    {
      "claim": "可验证事实，用中文",
      "source_ids": ["S1"],
      "confidence": "high|medium|low",
      "why_relevant": "它为什么影响日报判断"
    }
  ],
  "community_signals": [
    {
      "signal": "社区/用户/开发者反馈或讨论",
      "source_ids": ["S2"],
      "confidence": "high|medium|low"
    }
  ],
  "adoption_or_product_signals": [
    {
      "signal": "下载、安装、Demo、客户、集成、模板、文档成熟度等信号",
      "source_ids": ["S3"],
      "confidence": "high|medium|low"
    }
  ],
  "novelty_and_creativity_notes": [
    {
      "note": "这个项目有趣/新颖/可借鉴的具体点",
      "source_ids": ["S1"],
      "confidence": "high|medium|low"
    }
  ],
  "risks_and_limitations": [
    {
      "risk": "限制、争议、维护风险、许可风险、夸大风险",
      "source_ids": ["S1"],
      "confidence": "high|medium|low"
    }
  ],
  "unverified_claims": ["无法确认但可能重要的信息"],
  "open_questions": ["下一轮应该继续查的问题"],
  "next_search_directions": ["具体到搜索词/站点的延伸搜索方向"]
}
""".strip()

    if round_no == 1:
        user = f"""
请对下面 GitHub 项目做第 1 轮信息扩展搜索。必须联网，必须让全部可用 agent 并行搜索，并且只收录有网页 URL 支撑的额外信息。

项目上下文 JSON：
{json.dumps(project, ensure_ascii=False, indent=2)}

第 1 轮重点：
1. 项目是什么、解决什么问题、是否有官网/文档/Demo/package。
2. 作者或组织背景。
3. 最近为什么值得关注；寻找 release、提交、文章、社区讨论、产品发布等可验证原因。
4. 有无真实用户反馈、第三方讨论、安装/下载/集成迹象。
5. 创意点：为什么它可能对独立开发者、内容创作者或产品选题有启发。
""".strip()
    else:
        user = f"""
请基于第 1 轮搜索包和审核意见做第 2 轮延伸搜索。必须联网，必须让全部可用 agent 并行搜索，并且只收录有网页 URL 支撑的新增信息。

项目上下文 JSON：
{json.dumps(project, ensure_ascii=False, indent=2)}

第 1 轮搜索包 JSON：
{json.dumps(previous_pack or {}, ensure_ascii=False, indent=2)}

第 1 轮审核结果 JSON：
{json.dumps(review_result or {}, ensure_ascii=False, indent=2)}

第 2 轮重点：
1. 优先补齐审核 AI 指出的 rejected/needs_more_evidence/open_questions。
2. 对第 1 轮最重要的事实找第二来源或更权威来源。
3. 深挖竞品/同类项目、社区评价、使用门槛、争议、许可、安全和维护风险。
4. 如果第 1 轮已经足够，仍要扩展“这个项目为什么值得写”的背景和可借鉴方向。
5. 不要重复第 1 轮已经列出的同 URL 信息，除非用于修正错误。
""".strip()

    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_review_messages(project: dict[str, Any], *, round_no: Literal[1, 2], search_pack: dict[str, Any], previous_packs: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """审核 AI 提示词：逐条核对来源内容是否相关、是否能支撑事实。"""

    system = """
你是 GitHub 项目信息扩展的审核 AI。你不会联网搜索新资料；你的职责是只根据搜索 AI 给出的来源 URL、来源说明、服务端抓取到的网页标题/meta/正文片段，以及项目上下文，审核每条事实是否真的和目标 GitHub 项目相关、是否可被网页内容支持。

审核原则：
1. 没有 source_ids 的事实必须拒绝。
2. 你会收到 source_content_evidence；必须查看对应 source_id 的 title、meta_description、text_excerpt。
3. 如果网页内容片段没有出现项目名、仓库名、作者/组织名、官网域名、包名、论文名或其他强关联线索，来源必须标为 unrelated 或 weak，不能支撑核心事实。
4. 如果网页内容片段和目标项目相关，但不足以支撑 claim 的具体表述，必须拒绝或要求改写。
5. 如果只抓取失败、403/429、空正文，不能直接判定为相关；最多保留为 needs_manual_check/weak，不能支撑高置信事实。
6. 搜索结果中出现“可能、据称、似乎、有人说”等不确定表达时，不能进入 accepted_facts。
7. 明显来自 README/GitHub 的项目自述可以保留，但要标注为 self_claim/source_limit。
8. 社区反馈必须来自真实网页内容片段；不能把 README 文案当成社区反馈。
9. 不评价文风，只评价事实、项目相关性、证据支撑和下一轮搜索方向。
10. 除网页标题、URL、项目名、包名、专有名词和必要英文原文外，所有自然语言字段必须使用中文。
11. 输出必须是严格 JSON object，不要 Markdown，不要代码块。

JSON 结构必须为：
{
  "round": 1,
  "verdict": "pass|revise",
  "source_audit": [
    {
      "source_id": "S1",
      "url": "https://...",
      "status": "usable|weak|unrelated|reject",
      "project_relevance": "high|medium|low|none|unknown",
      "evidence_support": "supports_claim|partially_supports|does_not_support|not_enough_content",
      "reason": "说明网页片段里哪些内容能/不能证明它和目标 GitHub 项目相关，以及能/不能支撑哪些 claim"
    }
  ],
  "accepted_facts": [
    {
      "claim": "审核通过的事实",
      "source_ids": ["S1"],
      "confidence": "high|medium|low",
      "limitations": "来源局限；没有则为空字符串"
    }
  ],
  "rejected_or_rewrite_needed": [
    {
      "claim": "被拒绝或需要改写的内容",
      "reason": "为什么不成立",
      "suggested_fix": "如何改写或下一步怎么查"
    }
  ],
  "dedupe_notes": ["重复或可合并的信息"],
  "missing_evidence": ["还缺什么证据"],
  "next_round_questions": ["下一轮搜索必须回答的问题"],
  "safe_summary_for_next_round": "给下一轮搜索 AI 的简洁审核结论"
}
""".strip()

    user = f"""
请审核第 {round_no} 轮搜索 AI 的结果。只核查网页内容片段是否与目标 GitHub 项目相关、是否能支撑对应事实，不要新增事实。

项目上下文 JSON：
{json.dumps(project, ensure_ascii=False, indent=2)}

已有通过/历史搜索包 JSON：
{json.dumps(previous_packs or [], ensure_ascii=False, indent=2)}

本轮搜索包 JSON：
{json.dumps(search_pack, ensure_ascii=False, indent=2)}

请重点查看本轮搜索包里的 source_content_evidence。若所有核心事实都有“内容相关且能支撑 claim”的来源，verdict=pass；否则 verdict=revise，同时给出下一轮要补查的问题。
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_summary_messages(
    project: dict[str, Any],
    round1: dict[str, Any],
    review1: dict[str, Any],
    round2: dict[str, Any],
    review2: dict[str, Any],
    repo_ingest: dict[str, Any] | None = None,
    community_signals: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """摘要 AI 提示词：只综合审核通过的信息写结构化摘要。"""

    system = """
你是 GitHub 创意项目日报的摘要撰写 AI。你会收到项目上下文、两轮搜索包、两轮审核结果、仓库代码/结构速览，以及社区/传播信号爬虫结果。你的任务是先判断“这到底是什么项目”，再把可信信息综合成可进入日报的结构化中文摘要。

工具/仓库阅读要求：
1. 如果你的运行环境暴露了 mcp-git-ingest 工具，你必须先调用它读取目标 GitHub 仓库，再写摘要。
2. 输入里的 repo_ingest_context 通常已经由服务端通过用户本地 Mcp-git-ingest MCP 读取；如果 tool=mcp-git-ingest-local，必须优先参考其中的 directory_tree 和 file_excerpts。
3. 如果 repo_ingest_context 是 fallback，也必须作为仓库结构依据使用。
4. one_liner 和 daily_report_paragraph 必须优先参考仓库 README、目录结构、配置文件、examples/docs/src 等代码线索，避免只根据项目名猜用途。

写作原则：
1. 只使用审核 AI accepted_facts 中通过的事实、项目上下文中的 GitHub/README 原始事实，以及 community_signals_context 中带明确 URL 且 source_type 不是 fallback 的爬虫信号。
2. 搜索 AI 提到但审核未通过的信息，不得写成确定事实；community_signals_context 中 source_type=fallback 或 relevance_reason 明确提示“人工复核”的内容也不得写成确定事实，最多放入“待验证”。
3. 每个关键事实都要保留 source_ids 或 URL，便于后续追溯。
4. 最终给用户看的摘要不是审计报告，不要以“但仍需验证/当前未观察到/属于项目方自述/缺少证据”这类质疑句收尾。
5. 风险、限制、缺证据、项目方自述等内容只放进 risks_and_limitations 或 unverified_or_missing；除非涉及安全、违法、恶意软件、诈骗等高风险，不要写进 daily_report_paragraph。
6. daily_report_paragraph 要用清楚但不口水的话说明：这个项目是做什么的、解决什么问题、为什么现在值得关注、普通开发者或产品人能从中看到什么。
7. 如果缺少外部社区反馈，不要在摘要段落里强调“未观察到足够可验证外部反馈”；可以简单不写社区反馈，也不要机械复述榜单名次、stars 数或 README 统计来凑“热度理由”。
8. 语气要像科技日报导语：清楚、具体、有信息量，但不要唱衰、不要审稿式泼冷水。
9. 除网页标题、URL、项目名、包名、专有名词和必要英文原文外，所有自然语言字段必须使用中文。
10. 输出必须是严格 JSON object，不要 Markdown，不要代码块。

写法要求：
- one_liner 必须先直接回答“它是什么”，优先写项目本质类别 + 核心动作 + 目标对象/场景，而不是先强调“开源”。
- 除非“开源/闭源”本身就是理解项目的关键信息，否则不要把“这是一个开源的……”当成固定开头。
- 更好的写法示例：
  - “Headroom 是给 LLM 用的上下文压缩层，用来减少提示词 token。”
  - “MarkItDown 用来把 PDF、Office、网页等文件转成适合 LLM 处理的 Markdown。”
  - “ECC 是一套 AI 编码工作流 harness，用来统一代理的技能、规则、记忆和安全流程。”
- 避免这种机械写法：
  - “X 是一个开源的……，用于……”
  - “值得关注的是，X 是一个……”
  - “这是一个非常有趣的……”
- daily_report_paragraph 第一段先解释项目用途，再解释它火在哪；不要先讲融资、争议、缺证据或泛泛背景。
- 不要使用“神器、爆火、强势、颠覆、黑科技”等营销词。
- 可以具体但要短：说清对象、动作、结果，例如“把浏览器操作录成可复用脚本”“让 LLM 在长对话里保持记忆”“用 Rust 重写某类服务端组件”。

JSON 结构必须为：
{
  "project": "owner/repo",
  "one_liner": "一句话直接说明这个项目是什么，60 字以内；先说类别和用途，不要默认写成“这是一个开源的……”",
  "why_trending_today": "为什么现在值得关注；优先写可验证的发布、更新、能力变化、讨论背景，不要机械复述榜单名次或 stars 数",
  "what_it_does": ["功能点"],
  "creative_angle": ["有趣/创意/可借鉴点"],
  "user_scenarios": ["可能使用场景"],
  "developer_takeaways": ["独立开发者可借鉴方向"],
  "evidence_based_growth_reasons": [
    {"reason": "增长/传播原因", "source_ids": ["S1"], "confidence": "high|medium|low"}
  ],
  "community_feedback": [
    {"feedback": "社区反馈", "source_ids": ["S2"], "confidence": "high|medium|low"}
  ],
  "risks_and_limitations": [
    {"risk": "风险或限制", "source_ids": ["S1"], "confidence": "high|medium|low"}
  ],
  "verified_sources": [
    {"source_id": "S1", "title": "标题", "url": "https://...", "used_for": "用于支持什么"}
  ],
  "unverified_or_missing": ["仍需人工核查的信息"],
  "daily_report_paragraph": "可直接放入日报的一段中文摘要，180-280 字；先讲项目做什么，再讲火在哪和为什么值得看；不要口水话，不要质疑式收尾"
}
""".strip()

    user = f"""
请基于下面材料撰写最终结构化摘要。只使用审核通过的信息；未通过的信息放入待验证。

特别注意：
- one_liner 的第一分句必须直接回答“这是什么项目”，不要先说“开源项目/热门项目/GitHub 项目”。
- daily_report_paragraph 是给读者看的日报摘要，不是审核意见。
- 不要在 daily_report_paragraph 里集中罗列“缺证据、项目方自述、未观察到社区反馈”等质疑。
- 读者最需要知道的是：这个项目是什么、具体干什么、为什么突然值得关注、它的亮点/使用场景是什么。
- 请优先用 repo_ingest_context 和 README/代码结构来确认项目用途，不要只根据仓库名发挥。
- community_signals_context 是爬虫从 Hacker News、Reddit、Linux.do、Product Hunt、博客/Newsletter 等来源抓到的社区传播信号；只有 URL、标题、摘录与项目关键词能对应时才使用。

项目上下文 JSON：
{json.dumps(project, ensure_ascii=False, indent=2)}

repo_ingest_context JSON：
{json.dumps(repo_ingest or {}, ensure_ascii=False, indent=2)}

community_signals_context JSON：
{json.dumps(community_signals or {}, ensure_ascii=False, indent=2)}

第 1 轮搜索包 JSON：
{json.dumps(round1, ensure_ascii=False, indent=2)}

第 1 轮审核结果 JSON：
{json.dumps(review1, ensure_ascii=False, indent=2)}

第 2 轮搜索包 JSON：
{json.dumps(round2, ensure_ascii=False, indent=2)}

第 2 轮审核结果 JSON：
{json.dumps(review2, ensure_ascii=False, indent=2)}

请输出严格 JSON。
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _merge_expansion_payload(existing: dict[str, Any], expansion: dict[str, Any]) -> dict[str, Any]:
    payload = dict(existing)
    payload["github_info_expansion"] = expansion
    return payload


async def expand_github_project_info(item: SourceItemModel) -> dict[str, Any]:
    """对单个 GitHub 项目执行：搜索→审核→延伸搜索→审核→摘要。"""

    settings = get_settings()
    search_endpoint, review_endpoint, summary_endpoint = _settings_endpoints()
    project = build_project_context(item)

    round1_text, round1, round1_search_model = await _chat_json_object(
        search_endpoint,
        build_search_messages(project, round_no=1),
        stage="round1_search",
        serialize_gpt=False,
    )
    round1 = await _annotate_source_content_evidence(round1)

    review1_text, review1, review1_model = await _chat_json_object(
        review_endpoint,
        build_review_messages(project, round_no=1, search_pack=round1),
        stage="round1_review",
        serialize_gpt=True,
    )

    round2_text, round2, round2_search_model = await _chat_json_object(
        search_endpoint,
        build_search_messages(project, round_no=2, previous_pack=round1, review_result=review1),
        stage="round2_search",
        serialize_gpt=False,
    )
    round2 = await _annotate_source_content_evidence(round2)

    review2_text, review2, review2_model = await _chat_json_object(
        review_endpoint,
        build_review_messages(project, round_no=2, search_pack=round2, previous_packs=[round1, review1]),
        stage="round2_review",
        serialize_gpt=True,
    )

    repo_ingest = await _github_repo_ingest(project)
    if settings.enable_github_info_community_signals:
        try:
            community_signals = await expand_community_signals(
                project,
                repo_ingest,
                max_results_per_platform=settings.community_signal_max_results_per_platform,
                timeout_seconds=settings.community_signal_timeout_seconds,
                linux_do_cookie_header=settings.linux_do_cookie_header,
            )
        except Exception as exc:
            logger.debug("community signal expansion failed", exc_info=True)
            community_signals = {
                "version": "all_community_signal_expansion_v1",
                "expanded_at": _utc_now_iso(),
                "status": "failed",
                "error": type(exc).__name__,
                "error_message": str(exc)[:1000],
            }
    else:
        community_signals = {
            "version": "all_community_signal_expansion_v1",
            "expanded_at": _utc_now_iso(),
            "status": "disabled",
            "reason": "ENABLE_GITHUB_INFO_COMMUNITY_SIGNALS=false",
        }

    summary_text, summary, summary_model = await _chat_json_object(
        summary_endpoint,
        build_summary_messages(project, round1, review1, round2, review2, repo_ingest, community_signals),
        stage="final_summary",
        serialize_gpt=True,
    )

    return {
        "version": GITHUB_INFO_EXPANSION_VERSION,
        "expanded_at": _utc_now_iso(),
        "models": {
            "search": format_model_candidates(search_endpoint.model),
            "review": format_model_candidates(review_endpoint.model),
            "summary": format_model_candidates(summary_endpoint.model),
        },
        "project": {
            "source_item_id": item.source_item_id,
            "title": item.title,
            "url": item.url,
        },
        "repo_ingest_context": repo_ingest,
        "community_signals_context": community_signals,
        "agent_outputs": {
            "round1_search": {
                "agent": "search",
                "model": round1_search_model,
                "raw_content": round1_text,
            },
            "round1_review": {
                "agent": "review",
                "model": review1_model,
                "raw_content": review1_text,
            },
            "round2_search": {
                "agent": "search",
                "model": round2_search_model,
                "raw_content": round2_text,
            },
            "round2_review": {
                "agent": "review",
                "model": review2_model,
                "raw_content": review2_text,
            },
            "community_signal_crawlers": {
                "agent": "crawler",
                "model": "community_signal_expansion",
                "raw_content": _dump_json(community_signals),
            },
            "final_summary": {
                "agent": "summary",
                "model": summary_model,
                "raw_content": summary_text,
            },
        },
        "round1_search": round1,
        "round1_review": review1,
        "round2_search": round2,
        "round2_review": review2,
        "final_summary": summary,
    }


async def expand_and_save_github_project_info(db: Session, item_id: int) -> dict[str, Any]:
    item = db.get(SourceItemModel, item_id)
    if item is None:
        raise ValueError(f"source item not found: {item_id}")
    if item.source not in {"github_trending_daily", "github"}:
        raise ValueError(f"source item is not GitHub source: {item.source}")

    expansion = await expand_github_project_info(item)
    raw_payload = _safe_json_loads(item.raw_payload_json, {})
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    item.raw_payload_json = _dump_json(_merge_expansion_payload(raw_payload, expansion))

    metrics = _safe_json_loads(item.metrics_json, {})
    if not isinstance(metrics, dict):
        metrics = {}
    metrics["github_info_expansion_version"] = GITHUB_INFO_EXPANSION_VERSION
    metrics["github_info_expanded_at"] = expansion["expanded_at"]
    item.metrics_json = _dump_json(metrics)
    db.add(item)
    db.commit()
    db.refresh(item)
    return expansion


async def expand_and_save_community_signals(db: Session, item_id: int) -> dict[str, Any]:
    """只执行社区/传播信号爬虫并保存，避免依赖完整 LLM 流水线。"""

    item = db.get(SourceItemModel, item_id)
    if item is None:
        raise ValueError(f"source item not found: {item_id}")
    if item.source not in {"github_trending_daily", "github"}:
        raise ValueError(f"source item is not GitHub source: {item.source}")

    settings = get_settings()
    project = build_project_context(item)
    result = await expand_community_signals(
        project,
        max_results_per_platform=settings.community_signal_max_results_per_platform,
        timeout_seconds=settings.community_signal_timeout_seconds,
        linux_do_cookie_header=settings.linux_do_cookie_header,
        include_fallback_signals=True,
    )

    raw_payload = _safe_json_loads(item.raw_payload_json, {})
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    raw_payload["github_community_signals"] = result
    item.raw_payload_json = _dump_json(raw_payload)

    metrics = _safe_json_loads(item.metrics_json, {})
    if not isinstance(metrics, dict):
        metrics = {}
    metrics["github_community_signals_version"] = result.get("version")
    metrics["github_community_signals_expanded_at"] = result.get("expanded_at")
    metrics["github_community_signal_count"] = len(result.get("signals") or []) if isinstance(result, dict) else 0
    item.metrics_json = _dump_json(metrics)

    db.add(item)
    db.commit()
    db.refresh(item)
    return result


def latest_github_trending_items(db: Session, *, limit: int = 5) -> list[SourceItemModel]:
    stmt = (
        select(SourceItemModel)
        .where(SourceItemModel.source == "github_trending_daily")
        .order_by(desc(SourceItemModel.updated_at), desc(SourceItemModel.created_at))
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())
