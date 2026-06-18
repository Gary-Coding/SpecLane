from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .io_utils import read_json
from .time_utils import format_duration, now_iso, parse_iso_datetime


def data_artifact_path(config: dict[str, Any], name: str, session_meta: dict[str, Any]) -> Path:
    return Path(str(session_meta["data_dir"])) / name


def report_artifact_path(config: dict[str, Any], name: str, session_meta: dict[str, Any]) -> Path:
    return Path(str(session_meta["report_dir"])) / name


def workspace_relative_path(config: dict[str, Any], path: Path | str) -> str:
    resolved = Path(str(path)).resolve()
    root = Path(str(config.get("__workspace_root", ""))).resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return resolved.name


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def workflow_duration_seconds(
    session_meta: dict[str, Any],
    status: dict[str, Any] | None = None,
    finished_at: str | None = None,
) -> float:
    start_text = (
        str((status or {}).get("started_at", "")).strip()
        or str(session_meta.get("started_at", "")).strip()
        or str(session_meta.get("created_at", "")).strip()
    )
    end_text = str(finished_at or (status or {}).get("finished_at", "")).strip() or now_iso()
    started = parse_iso_datetime(start_text)
    ended = parse_iso_datetime(end_text)
    if started is None or ended is None:
        return 0.0
    return max(0.0, (ended - started).total_seconds())


def _assert_managed_notification_path(config: dict[str, Any], path: Path) -> Path:
    resolved = path.resolve()
    data_root = Path(str(session_meta_data_root := path.parent)).resolve()
    workspace_root = Path(str(config.get("__workspace_root", ""))).resolve()
    if workspace_root and workspace_root in resolved.parents:
        return resolved
    return data_root / path.name

def pushplus_config(config: dict[str, Any]) -> dict[str, Any]:
    notification = config.get("notification", {})
    if not isinstance(notification, dict):
        return {
            "token": "",
            "routes": [],
        }
    pushplus = notification.get("pushplus", {})
    if not isinstance(pushplus, dict):
        return {
            "token": "",
            "routes": [],
        }

    ordinary = pushplus.get("ordinary", {})
    if not isinstance(ordinary, dict):
        ordinary = {}
    return {
        "token": str(pushplus.get("token", "")).strip(),
        "routes": [
            {
                "name": "ordinary",
                "enabled": bool(ordinary.get("enabled", False)),
                "channel": str(ordinary.get("channel", "wechat")).strip() or "wechat",
                "template": str(ordinary.get("template", "markdown")).strip() or "markdown",
            },
        ],
    }


def feishu_config(config: dict[str, Any]) -> dict[str, Any]:
    notification = config.get("notification", {})
    if not isinstance(notification, dict):
        return {
            "enabled": False,
            "webhook_url": "",
            "secret": "",
        }
    feishu = notification.get("feishu", {})
    if not isinstance(feishu, dict):
        return {
            "enabled": False,
            "webhook_url": "",
            "secret": "",
        }
    return {
        "enabled": bool(feishu.get("enabled", False)),
        "webhook_url": str(feishu.get("webhook_url", "")).strip(),
        "secret": str(feishu.get("secret", "")).strip(),
    }


def pushplus_api_url() -> str:
    return str(os.environ.get("SUPER_ENGINEER_PUSHPLUS_URL", "https://www.pushplus.plus/send")).strip()


def pushplus_request_url(token: str) -> str:
    base = pushplus_api_url().rstrip("/")
    return f"{base}/{token}" if token else base


def _normalize_pushplus_response(status_code: int, body: str, route: dict[str, Any], sender: str) -> dict[str, Any]:
    try:
        response_payload = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        response_payload = {"raw": body}
    response_code = response_payload.get("code")
    success = status_code == 200 and response_code in (None, 0, 200, "0", "200")
    return {
        "route": route.get("name", ""),
        "channel": route.get("channel", ""),
        "template": route.get("template", ""),
        "sender": sender,
        "status": "sent" if success else "failed",
        "success": success,
        "message": str(response_payload.get("msg") or ("发送成功" if success else "发送失败")),
        "response": response_payload,
    }


def send_pushplus_notification_python(payload: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    request = urllib_request.Request(
        pushplus_api_url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", 200)
    except urllib_error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        result = _normalize_pushplus_response(error.code, body, route, "python")
        result["message"] = str(result.get("message") or f"PushPlus HTTP {error.code}")
        return result
    except urllib_error.URLError as error:
        return {
            "route": route.get("name", ""),
            "channel": route.get("channel", ""),
            "template": route.get("template", ""),
            "sender": "python",
            "status": "failed",
            "success": False,
            "message": f"PushPlus 请求失败：{error.reason}",
            "response": {},
        }
    return _normalize_pushplus_response(status_code, body, route, "python")


def send_pushplus_notification_curl(payload: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    curl_path = shutil.which("curl")
    if not curl_path:
        return {
            "route": route.get("name", ""),
            "channel": route.get("channel", ""),
            "template": route.get("template", ""),
            "sender": "curl",
            "status": "failed",
            "success": False,
            "message": "系统中未找到 curl，无法执行回退发送。",
            "response": {},
        }
    result = subprocess.run(
        [
            curl_path,
            "-sS",
            pushplus_api_url(),
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        return {
            "route": route.get("name", ""),
            "channel": route.get("channel", ""),
            "template": route.get("template", ""),
            "sender": "curl",
            "status": "failed",
            "success": False,
            "message": f"curl 回退发送失败：{result.stderr.strip() or result.stdout.strip() or result.returncode}",
            "response": {},
        }
    return _normalize_pushplus_response(200, result.stdout, route, "curl")


def send_pushplus_notification(pushplus: dict[str, Any], route: dict[str, Any], title: str, content: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "token": str(pushplus.get("token", "")).strip(),
        "title": title,
        "content": content,
        "template": route.get("template", "markdown"),
        "channel": route.get("channel", "wechat"),
    }
    python_result = send_pushplus_notification_python(payload, route)
    if python_result.get("success"):
        return python_result
    curl_result = send_pushplus_notification_curl(payload, route)
    if curl_result.get("success"):
        curl_result["message"] = f"{curl_result.get('message', '')}（Python失败后已回退curl成功）"
        curl_result["python_error"] = python_result.get("message", "")
        return curl_result
    curl_result["python_error"] = python_result.get("message", "")
    curl_result["message"] = (
        f"Python发送失败：{python_result.get('message', '')}；"
        f"curl回退也失败：{curl_result.get('message', '')}"
    )
    return curl_result


def _normalize_feishu_response(status_code: int, body: str, sender: str) -> dict[str, Any]:
    try:
        response_payload = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        response_payload = {"raw": body}
    response_code = response_payload.get("code")
    success = status_code == 200 and response_code in (None, 0, "0")
    return {
        "route": "feishu",
        "channel": "webhook",
        "template": "interactive",
        "sender": sender,
        "status": "sent" if success else "failed",
        "success": success,
        "message": str(response_payload.get("msg") or ("success" if success else "发送失败")),
        "response": response_payload,
    }


def feishu_sign(secret: str, timestamp: str) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _workflow_notification_title(session_id: str, overall_result: str) -> str:
    return "SpecLane workflow notification"


def _workflow_notification_status_text(status: dict[str, Any], overall_result: str) -> str:
    current_task = str(status.get("current_task", "") or "暂无").strip()
    next_action = str(status.get("next_action", "") or "工作流已完成，请前往工作区查看。").strip()
    if overall_result == "通过":
        current_task = current_task.replace("✅", "").replace("❌", "").rstrip("。")
        return f"{current_task} ✅。{next_action}"
    current_task = current_task.replace("✅", "").replace("❌", "").rstrip("。")
    return f"{current_task} ❌。{next_action}"


def workflow_notification_fingerprint(
    session_meta: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
) -> str:
    finished_at = str(status.get("finished_at", "") or "").strip()
    phase = str(status.get("phase", "") or "").strip()
    current_task = str(status.get("current_task", "") or "").strip()
    return "|".join(
        [
            str(session_meta.get("session_id", "")).strip(),
            overall_result.strip(),
            finished_at,
            phase,
            current_task,
        ]
    )


def notification_has_sent_route(notification: dict[str, Any], route: str, template: str) -> bool:
    results = notification.get("results", [])
    if not isinstance(results, list):
        return False
    for item in results:
        if not isinstance(item, dict):
            continue
        if str(item.get("route", "")).strip() != route:
            continue
        if str(item.get("template", "")).strip() != template:
            continue
        if str(item.get("status", "")).strip() == "sent" and item.get("success", True) is not False:
            return True
    return False


def is_standard_workflow_notification(
    config: dict[str, Any],
    session_meta: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
    notification: dict[str, Any],
) -> bool:
    if not isinstance(notification, dict):
        return False
    if str(notification.get("provider", "")).strip() != "notification":
        return False
    if str(notification.get("source", "")).strip() != "run-workflow.py verify":
        return False
    if str(notification.get("status", "")).strip() not in ("sent", "partial"):
        return False
    expected_fingerprint = workflow_notification_fingerprint(session_meta, status, overall_result)
    if str(notification.get("fingerprint", "")).strip() != expected_fingerprint:
        return False
    if feishu_config(config).get("enabled"):
        return notification_has_sent_route(notification, "feishu", "interactive")
    return str(notification.get("status", "")).strip() == "sent"


def build_workflow_notification(
    config: dict[str, Any],
    session_meta: dict[str, Any],
    plan: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
    template: str = "markdown",
) -> tuple[str, str]:
    duration_seconds = workflow_duration_seconds(session_meta, status, status.get("finished_at", ""))
    progress = plan.get("todo_progress", {})
    targets = [str(item.get("name", "")).strip() for item in plan.get("target_codebases", []) if str(item.get("name", "")).strip()]
    target_text = "、".join(targets) if targets else "未识别"
    title = _workflow_notification_title(session_meta["session_id"], overall_result)
    current_task = _workflow_notification_status_text(status, overall_result)
    phase_text = str(status.get("phase", "") or "unknown").strip()
    mode_text = str(config.get("mode", "manual")).strip()
    completed_count = progress.get("completed_task_count", 0)
    total_count = progress.get("total_task_count", 0)
    pending_count = progress.get("pending_task_count", 0)
    if overall_result == "通过" and phase_text == "done" and total_count:
        completed_count = total_count
        pending_count = 0

    if template == "html":
        lines = [
            "<div style=\"font-size:14px;line-height:1.7;\">",
            "<h2 style=\"margin:0 0 8px 0;font-size:16px;\">任务摘要</h2>",
            "<ul style=\"margin:0 0 16px 18px;padding:0;\">",
            f"<li>会话：<code>{session_meta['session_id']}</code></li>",
            f"<li>仓库：{target_text}</li>",
            f"<li>模式：<code>{mode_text}</code></li>",
            f"<li>耗时：{format_duration(duration_seconds)}</li>",
            f"<li>进度：{completed_count}/{total_count} 已完成，剩余 {pending_count}</li>",
            "</ul>",
            "<h2 style=\"margin:0 0 8px 0;font-size:16px;\">任务结果</h2>",
            "<ul style=\"margin:0 0 0 18px;padding:0;\">",
            f"<li>当前阶段：<code>{phase_text}</code></li>",
            f"<li>当前说明：{current_task}</li>",
            f"<li>下一步：{next_action}</li>",
            "</ul>",
            "</div>",
        ]
        return title, "".join(lines)

    if template == "txt":
        lines = [
            "【任务摘要】",
            f"会话：{session_meta['session_id']}",
            f"仓库：{target_text}",
            f"模式：{mode_text}｜耗时：{format_duration(duration_seconds)}",
            f"进度：{completed_count}/{total_count} 已完成，剩余 {pending_count}",
            "",
            "【任务结果】",
            f"阶段：{phase_text}",
            f"说明：{current_task or '暂无'}",
        ]
        return title, "\n".join(lines)

    lines = [
        "## 任务摘要",
        "",
        f"会话：`{session_meta['session_id']}`  ",
        f"仓库：{target_text}  ",
        f"模式：`{mode_text}`｜耗时：{format_duration(duration_seconds)}  ",
        f"进度：{completed_count}/{total_count} 已完成，剩余 {pending_count}",
        "",
        "## 任务结果",
        "",
        f"阶段：`{phase_text}`  ",
        f"说明：{current_task or '暂无'}",
    ]
    return title, "\n".join(lines)


def build_feishu_notification_payload(
    config: dict[str, Any],
    session_meta: dict[str, Any],
    plan: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
    title: str,
) -> dict[str, Any]:
    duration_seconds = workflow_duration_seconds(session_meta, status, status.get("finished_at", ""))
    progress = plan.get("todo_progress", {})
    targets = [str(item.get("name", "")).strip() for item in plan.get("target_codebases", []) if str(item.get("name", "")).strip()]
    target_text = "、".join(targets) if targets else "未识别"
    current_task = _workflow_notification_status_text(status, overall_result)
    phase_text = str(status.get("phase", "") or "unknown").strip()
    mode_text = str(config.get("mode", "manual")).strip()
    completed_count = progress.get("completed_task_count", 0)
    total_count = progress.get("total_task_count", 0)
    pending_count = progress.get("pending_task_count", 0)
    if overall_result == "通过" and phase_text == "done" and total_count:
        completed_count = total_count
        pending_count = 0
    status_emoji = "✅" if overall_result == "通过" else "❌"
    header_template = "green" if overall_result == "通过" else "red"
    reports = {
        "plan.md": workspace_relative_path(config, report_artifact_path(config, "plan.md", session_meta)),
        "review.md": workspace_relative_path(config, report_artifact_path(config, "review.md", session_meta)),
        "verify.md": workspace_relative_path(config, report_artifact_path(config, "verify.md", session_meta)),
    }
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {
                    "text_size": {
                        "normal_v2": {
                            "default": "normal",
                            "pc": "normal",
                            "mobile": "heading",
                        }
                    }
                },
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title,
                },
                "template": header_template,
                "padding": "12px 12px 12px 12px",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            "**任务摘要**\n"
                            f"- 会话：`{session_meta['session_id']}`\n"
                            f"- 仓库：{target_text}\n"
                            f"- 模式：`{mode_text}`\n"
                            f"- 耗时：{format_duration(duration_seconds)}\n"
                            f"- 进度：{completed_count}/{total_count} 已完成，剩余 {pending_count}"
                        ),
                        "text_align": "left",
                        "text_size": "normal_v2",
                    },
                    {
                        "tag": "markdown",
                        "content": (
                            "**任务结果**\n"
                            f"- 阶段：`{phase_text}`\n"
                            f"- 说明：{current_task or '暂无'}\n"
                            f"- 通知来源：`speclane verify`\n"
                            f"- 报告：`plan.md` / `review.md` / `verify.md`"
                        ),
                        "text_align": "left",
                        "text_size": "normal_v2",
                    },
                    {
                        "tag": "markdown",
                        "content": (
                            "**报告路径**\n"
                            f"- plan：`{reports['plan.md']}`\n"
                            f"- review：`{reports['review.md']}`\n"
                            f"- verify：`{reports['verify.md']}`"
                        ),
                        "text_align": "left",
                        "text_size": "normal_v2",
                    },
                ],
            },
        },
    }


def send_feishu_notification_python(webhook_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib_request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", 200)
    except urllib_error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        result = _normalize_feishu_response(error.code, body, "python")
        result["message"] = str(result.get("message") or f"飞书 HTTP {error.code}")
        return result
    except urllib_error.URLError as error:
        return {
            "route": "feishu",
            "channel": "webhook",
            "template": "interactive",
            "sender": "python",
            "status": "failed",
            "success": False,
            "message": f"飞书 webhook 请求失败：{error.reason}",
            "response": {},
        }
    return _normalize_feishu_response(status_code, body, "python")


def send_feishu_notification_curl(webhook_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    curl_path = shutil.which("curl")
    if not curl_path:
        return {
            "route": "feishu",
            "channel": "webhook",
            "template": "interactive",
            "sender": "curl",
            "status": "failed",
            "success": False,
            "message": "系统中未找到 curl，无法执行飞书回退发送。",
            "response": {},
        }
    result = subprocess.run(
        [
            curl_path,
            "-sS",
            webhook_url,
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        return {
            "route": "feishu",
            "channel": "webhook",
            "template": "interactive",
            "sender": "curl",
            "status": "failed",
            "success": False,
            "message": f"飞书 curl 回退发送失败：{result.stderr.strip() or result.stdout.strip() or result.returncode}",
            "response": {},
        }
    return _normalize_feishu_response(200, result.stdout, "curl")


def send_feishu_notification(
    feishu: dict[str, Any],
    config: dict[str, Any],
    session_meta: dict[str, Any],
    plan: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
) -> dict[str, Any]:
    title = _workflow_notification_title(session_meta["session_id"], overall_result)
    payload = build_feishu_notification_payload(config, session_meta, plan, status, overall_result, title)
    secret = str(feishu.get("secret", "")).strip()
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(secret, timestamp)
    webhook_url = str(feishu.get("webhook_url", "")).strip()
    python_result = send_feishu_notification_python(webhook_url, payload)
    if python_result.get("success"):
        return python_result
    curl_result = send_feishu_notification_curl(webhook_url, payload)
    if curl_result.get("success"):
        curl_result["message"] = f"{curl_result.get('message', '')}（Python失败后已回退curl成功）"
        curl_result["python_error"] = python_result.get("message", "")
        return curl_result
    curl_result["python_error"] = python_result.get("message", "")
    curl_result["message"] = (
        f"Python发送失败：{python_result.get('message', '')}；"
        f"curl回退也失败：{curl_result.get('message', '')}"
    )
    return curl_result


def notify_workflow_result(
    config: dict[str, Any],
    session_meta: dict[str, Any],
    plan: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
) -> dict[str, Any]:
    notification_path = data_artifact_path(config, "notification.json", session_meta)
    existing_result = read_json(notification_path, {})
    current_fingerprint = workflow_notification_fingerprint(session_meta, status, overall_result)
    if (
        isinstance(existing_result, dict)
        and is_standard_workflow_notification(config, session_meta, status, overall_result, existing_result)
    ):
        deduped_result = dict(existing_result)
        deduped_result["deduplicated"] = True
        deduped_result["message"] = "通知已发送，已跳过重复发送。"
        return deduped_result

    pushplus = pushplus_config(config)
    feishu = feishu_config(config)
    enabled_routes = [item for item in pushplus.get("routes", []) if item.get("enabled")]
    if feishu.get("enabled"):
        enabled_routes.append({"name": "feishu", "channel": "webhook", "template": "interactive"})
    result: dict[str, Any] = {
        "provider": "notification",
        "source": "run-workflow.py verify",
        "schema_version": 1,
        "fingerprint": current_fingerprint,
        "enabled": bool(enabled_routes),
        "status": "skipped",
        "message": "未配置通知。",
        "sent_at": now_iso(),
        "title": "",
        "routes": enabled_routes,
        "results": [],
    }
    if enabled_routes:
        route_results = []
        last_title = ""
        for route in enabled_routes:
            if route.get("name") == "feishu":
                last_title = _workflow_notification_title(session_meta["session_id"], overall_result)
                route_results.append(
                    send_feishu_notification(feishu, config, session_meta, plan, status, overall_result)
                )
                continue
            title, content = build_workflow_notification(
                config,
                session_meta,
                plan,
                status,
                overall_result,
                template=str(route.get("template", "markdown") or "markdown"),
            )
            last_title = title
            route_results.append(send_pushplus_notification(pushplus, route, title, content))
        sent_count = sum(1 for item in route_results if item.get("status") == "sent")
        if sent_count == len(route_results):
            result["status"] = "sent"
        elif sent_count > 0:
            result["status"] = "partial"
        else:
            result["status"] = "failed"
        result["title"] = last_title
        result["message"] = "；".join(
            f"{item.get('route', 'unknown')}:{item.get('message', '')}" for item in route_results
        )
        result["results"] = route_results
        result["sent_at"] = now_iso()
    write_json(notification_path, result)
    return result


