from __future__ import annotations

import re
from typing import Any


def parse_scalar(value: str) -> Any:
    stripped = value.strip()
    if stripped in ("", "null", "~"):
        return ""
    if stripped == "[]":
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        parts: list[str] = []
        current = ""
        quote = ""
        for char in inner:
            if char in ("'", '"'):
                if not quote:
                    quote = char
                elif quote == char:
                    quote = ""
                current += char
                continue
            if char == "," and not quote:
                parts.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            parts.append(current.strip())
        return [parse_scalar(part) for part in parts]
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        return stripped[1:-1]
    return stripped


def _tokenize_yaml(text: str) -> list[tuple[int, str]]:
    tokens: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        tokens.append((indent, raw_line.strip()))
    return tokens


def _parse_yaml_block(tokens: list[tuple[int, str]], index: int, indent: int) -> tuple[int, Any]:
    container: Any = None

    while index < len(tokens):
        current_indent, content = tokens[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError(f"YAML 缩进不合法：{content}")

        if content.startswith("- "):
            if container is None:
                container = []
            if not isinstance(container, list):
                raise ValueError("YAML 不能在同一层混用对象和数组。")
            value_part = content[2:].strip()
            if value_part:
                if ":" in value_part and not value_part.startswith(("'", '"')):
                    key, _, value = value_part.partition(":")
                    item: dict[str, Any] = {key.strip(): parse_scalar(value.strip())}
                    next_index = index + 1
                    if next_index < len(tokens) and tokens[next_index][0] > indent:
                        next_index, nested = _parse_yaml_block(tokens, next_index, indent + 2)
                        if isinstance(nested, dict):
                            item.update(nested)
                        else:
                            raise ValueError("YAML 数组对象后续缩进必须是对象。")
                    container.append(item)
                    index = next_index
                    continue
                container.append(parse_scalar(value_part))
                index += 1
                continue
            index, nested = _parse_yaml_block(tokens, index + 1, indent + 2)
            container.append(nested)
            continue

        if container is None:
            container = {}
        if not isinstance(container, dict):
            raise ValueError("YAML 不能在同一层混用数组和对象。")

        key, sep, value_part = content.partition(":")
        if not sep:
            raise ValueError(f"YAML 行缺少冒号：{content}")
        key = key.strip()
        value_part = value_part.strip()
        if value_part:
            container[key] = parse_scalar(value_part)
            index += 1
            continue
        index, nested = _parse_yaml_block(tokens, index + 1, indent + 2)
        container[key] = nested

    if container is None:
        return index, {}
    return index, container


def parse_simple_yaml(text: str) -> dict[str, Any]:
    tokens = _tokenize_yaml(text)
    if not tokens:
        return {}
    _, parsed = _parse_yaml_block(tokens, 0, tokens[0][0])
    if not isinstance(parsed, dict):
        raise ValueError("工作空间配置必须是对象结构。")
    return parsed
