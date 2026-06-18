from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .io_utils import read_text


def is_url(value: Any) -> bool:
    parsed = urlparse(str(value).strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_lark_doc_url(value: Any) -> bool:
    text = str(value).strip()
    if not is_url(text):
        return False
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not any(marker in host for marker in ("feishu.cn", "larksuite.com", "larksuite.cn")):
        return False
    return any(marker in path for marker in ("/doc", "/docx", "/wiki"))

def demand_path(config: dict[str, Any]) -> Path | None:
    path_text = str(config.get("demand_file", "")).strip()
    if not path_text or str(config.get("demand_source_type", "")).strip() == "lark_doc" or is_url(path_text):
        return None
    return Path(path_text).resolve()


def lark_cli_available() -> bool:
    return bool(shutil.which("lark-cli"))


def lark_cli_install_message() -> str:
    return "\n".join(
        [
            "检测到 demand_file 是飞书/Lark 云文档，但本机未安装官方 lark-cli。",
            "请先安装并完成授权：",
            "1. npx @larksuite/cli@latest install",
            "2. lark-cli config init --new",
            "3. lark-cli auth login --recommend",
            "4. lark-cli auth status",
            "安装和授权完成后重新执行 /sl:propose <change-name>。",
        ]
    )


def _extract_lark_cli_text(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    def pick(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("content", "markdown", "text", "body", "document", "doc"):
                picked = pick(value.get(key))
                if picked:
                    return picked
            for item in value.values():
                picked = pick(item)
                if picked:
                    return picked
        if isinstance(value, list):
            parts = [pick(item) for item in value]
            return "\n\n".join(item for item in parts if item)
        return ""

    return pick(parsed) or text


def read_lark_doc_demand(config: dict[str, Any], demand_url: str) -> dict[str, Any]:
    if not lark_cli_available():
        raise RuntimeError(lark_cli_install_message())
    commands = [
        ["lark-cli", "docs", "+fetch", "--api-version", "v2", "--doc", demand_url, "--doc-format", "markdown", "--detail", "full"],
        ["lark-cli", "docs", "+fetch", "--api-version", "v2", "--doc", demand_url, "--doc-format", "markdown"],
        ["lark-cli", "docs", "+fetch", "--doc", demand_url, "--doc-format", "markdown"],
        ["lark-cli", "docs", "+fetch", "--url", demand_url, "--doc-format", "markdown"],
    ]
    attempts: list[dict[str, Any]] = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=str(config.get("__workspace_root", os.getcwd())),
                text=True,
                capture_output=True,
                check=False,
                timeout=90,
            )
        except FileNotFoundError:
            raise RuntimeError(lark_cli_install_message())
        except subprocess.TimeoutExpired:
            attempts.append(
                {
                    "command": command,
                    "returncode": "timeout",
                    "stdout": "",
                    "stderr": "lark-cli docs +fetch timeout",
                }
            )
            continue
        attempts.append(
            {
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode == 0:
            content = _extract_lark_cli_text(result.stdout)
            if content:
                return {
                    "source_type": "lark_doc",
                    "source": demand_url,
                    "content": content,
                    "command": command,
                    "attempts": attempts,
                }
    last = attempts[-1] if attempts else {}
    raise RuntimeError(
        "读取飞书/Lark 云文档失败。请确认 lark-cli 已完成授权且当前账号有文档访问权限。\n"
        "建议执行：lark-cli auth status；如未登录，执行：lark-cli auth login --recommend。\n"
        f"最后一次错误：{str(last.get('stderr') or last.get('stdout') or '').strip()}"
    )


def read_demand_source(config: dict[str, Any]) -> dict[str, Any]:
    source = str(config.get("demand_file", "")).strip()
    if not source:
        return {"source_type": "", "source": "", "content": "", "command": [], "attempts": []}
    if str(config.get("demand_source_type", "")).strip() == "lark_doc" or is_lark_doc_url(source):
        return read_lark_doc_demand(config, source)
    path = Path(source)
    return {
        "source_type": "local",
        "source": str(path.resolve()),
        "content": read_text(path),
        "command": [],
        "attempts": [],
    }


