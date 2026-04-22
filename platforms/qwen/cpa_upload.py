from __future__ import annotations

import json
import sys
import types
from typing import Tuple

try:
    from curl_cffi import requests as cffi_requests
except ModuleNotFoundError:
    import requests as cffi_requests

    curl_cffi_module = types.ModuleType("curl_cffi")
    curl_cffi_module.requests = cffi_requests
    sys.modules.setdefault("curl_cffi", curl_cffi_module)


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "")
    except Exception:
        return ""


def generate_token_json(account) -> dict:
    return {
        "type": "qwen",
        "provider": "qwen",
        "email": getattr(account, "email", ""),
        "access_token": getattr(account, "access_token", ""),
        "refresh_token": getattr(account, "refresh_token", ""),
        "resource_url": getattr(account, "resource_url", ""),
    }


def upload_to_cpa(
    token_data: dict,
    api_url: str = None,
    api_key: str = None,
    proxy: str = None,
) -> Tuple[bool, str]:
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    if not refresh_token:
        return False, "Qwen CPA 上传需要 refresh_token"

    api_url = str(api_url or _get_config_value("cpa_api_url") or "").strip()
    api_key = str(api_key or _get_config_value("cpa_api_key") or "").strip()
    if not api_url:
        return False, "CPA API URL 未配置"

    upload_url = f"{api_url.rstrip('/')}/v0/management/auth-files"
    filename = f"{token_data.get('email') or 'qwen'}.json"
    files = {
        "file": (
            filename,
            json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json",
        )
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    request_kwargs = {
        "headers": headers,
        "files": files,
        "timeout": 30,
        "impersonate": "chrome124",
    }
    if proxy:
        request_kwargs["proxies"] = {"http": proxy, "https": proxy}

    try:
        response = cffi_requests.post(upload_url, **request_kwargs)
    except Exception as exc:
        return False, str(exc)

    if response.status_code in (200, 201):
        return True, "上传成功"

    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or "").strip()
    except Exception:
        detail = ""
    if not detail:
        detail = (response.text or "")[:200]
    return False, detail or f"上传失败: HTTP {response.status_code}"
