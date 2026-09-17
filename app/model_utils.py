from __future__ import annotations

import re
from collections.abc import Iterable

_MODEL_SPLIT_RE = re.compile(r"[\s,，;；|]+")
_MODEL_COMPAT_FALLBACKS: dict[str, tuple[str, ...]] = {
    "gpt-5.5": ("gpt-5.4",),
}


def parse_model_candidates(value: str | None, *, fallback: Iterable[str] | None = None) -> list[str]:
    """把逗号或空白分隔的模型配置解析成候选列表并去重。

    当前会为部分模型自动补充兼容回退链，例如：
    - gpt-5.5 -> gpt-5.4
    """

    raw_values: list[str] = []
    if value:
        raw_values.extend(_MODEL_SPLIT_RE.split(value.strip()))
    if fallback:
        raw_values.extend(str(item).strip() for item in fallback)

    seen: set[str] = set()
    result: list[str] = []
    for item in raw_values:
        model = item.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        result.append(model)

    for model in list(result):
        for fallback_model in _MODEL_COMPAT_FALLBACKS.get(model, ()):
            if not fallback_model or fallback_model in seen:
                continue
            seen.add(fallback_model)
            result.append(fallback_model)
    return result


def first_model_candidate(value: str | None, *, fallback: Iterable[str] | None = None, default: str = "") -> str:
    candidates = parse_model_candidates(value, fallback=fallback)
    if candidates:
        return candidates[0]
    return default


def format_model_candidates(value: str | Iterable[str] | None, *, separator: str = ", ") -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        candidates = parse_model_candidates(value)
    else:
        candidates = parse_model_candidates(None, fallback=value)
    return separator.join(candidates)
