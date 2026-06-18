from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def markdown_headings(text: str, limit: int = 16) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            headings.append(stripped[:160])
        if len(headings) >= limit:
            break
    return headings


def compact_text_excerpt(text: str, keywords: list[str] | None = None, max_chars: int = 6000) -> str:
    normalized = text.strip()
    if len(normalized) <= max_chars:
        return normalized

    keywords = [item.lower() for item in (keywords or []) if item]
    lines = normalized.splitlines()
    selected: list[str] = []
    selected.extend(lines[:60])
    keyword_snippets: list[str] = []
    for index, line in enumerate(lines):
        lowered = line.lower()
        if keywords and not any(keyword in lowered for keyword in keywords):
            continue
        start = max(0, index - 2)
        end = min(len(lines), index + 3)
        snippet = ["", *lines[start:end]]
        keyword_snippets.extend(snippet)
        selected.extend(snippet)
        if len("\n".join(selected)) >= max_chars:
            break
    excerpt = "\n".join(selected).strip()
    if len(excerpt) > max_chars:
        if keyword_snippets:
            head_budget = max(80, max_chars // 2)
            tail_budget = max(80, max_chars - head_budget - 16)
            head = "\n".join(lines[:60]).strip()[:head_budget].rstrip()
            tail = "\n".join(keyword_snippets).strip()
            if len(tail) > tail_budget:
                tail = tail[:tail_budget].rstrip()
            excerpt = f"{head}\n\n...\n\n{tail}".strip()
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars].rstrip()
    return excerpt + "\n\n...[已摘要，按需读取原文件全文]..."


def summarize_markdown_file(path: Path, keywords: list[str] | None = None, max_excerpt_chars: int = 6000) -> dict[str, Any]:
    text = read_text(path)
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size if path.exists() else 0,
        "sha256": file_sha256(path),
        "headings": markdown_headings(text),
        "excerpt": compact_text_excerpt(text, keywords=keywords, max_chars=max_excerpt_chars),
        "truncated": len(text.strip()) > max_excerpt_chars,
    }


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return fallback
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def relative_to(path: str | Path, root: Path) -> str:
    candidate = Path(str(path)).resolve()
    try:
        return str(candidate.relative_to(root.resolve()))
    except ValueError:
        return str(candidate)
