from __future__ import annotations

import base64
import json
import random
import re
import string
import sys
import time
import types
from dataclasses import dataclass
from html import unescape
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

try:
    from curl_cffi import requests as curl_requests
except ModuleNotFoundError:
    import requests as curl_requests

    curl_cffi_module = types.ModuleType("curl_cffi")
    curl_cffi_module.requests = curl_requests
    sys.modules.setdefault("curl_cffi", curl_cffi_module)

from core.base_mailbox import MailboxAccount

QWEN_BASE_URL = "https://chat.qwen.ai"
ACTIVATION_PATH = "/api/v1/auths/activate"
_ACTIVATION_URL_PATTERN = re.compile(r"https://chat\.qwen\.ai/api/v1/auths/activate\?[^\s\"'<>]+")


@dataclass
class QwenOAuthTokens:
    oauth_access_token: str = ""
    refresh_token: str = ""
    resource_url: str = ""


class QwenRegister:
    def __init__(self, executor: Any, log_fn: Optional[Callable[[str], None]] = None):
        self.executor = executor
        self._log_fn = log_fn or (lambda _message: None)

    def _log(self, message: str) -> None:
        try:
            self._log_fn(message)
        except Exception:
            pass

    def register(self, email: str, password: str, full_name: str = "") -> dict:
        if not full_name:
            full_name = _default_full_name(email)

        last_result: dict[str, Any] = {
            "status": "failed",
            "email": email,
            "password": password,
            "full_name": full_name,
            "tokens": {},
            "error": "register did not start",
        }
        page = getattr(self.executor, "page", None)

        for attempt in range(1, 4):
            result = self._try_register(page, email, password, full_name)
            if not isinstance(result, dict):
                result = {"status": "failed", "tokens": {}, "error": "invalid result"}
            result.setdefault("email", email)
            result.setdefault("password", password)
            result.setdefault("full_name", full_name)
            tokens = result.get("tokens")
            if not isinstance(tokens, dict):
                tokens = {}
                result["tokens"] = tokens
            token = str(tokens.get("cookie:token") or "").strip()
            if result.get("status") == "success" and token:
                if attempt == 1:
                    self._log("first-attempt token hit")
                else:
                    self._log(f"token hit on retry #{attempt}")
                return result
            last_result = result
            if attempt < 3:
                time.sleep(1)

        error_message = str(last_result.get("error") or "unknown")
        self._log(f"final failure reason(no token): {error_message}")
        last_result.setdefault("status", "failed")
        return last_result

    def _try_register(self, page: Any, email: str, password: str, full_name: str) -> dict:
        raise NotImplementedError("Qwen real registration flow is not implemented yet")


def wait_for_activation_link(mailbox: Any, account_email: str, timeout: int = 120) -> Optional[str]:
    timeout_seconds = max(int(timeout or 0), 1)
    deadline = time.monotonic() + timeout_seconds
    seen_ids: set[Any] = set()

    while time.monotonic() < deadline:
        for mail in _list_mails_for_email(mailbox, account_email):
            mail_id = mail.get("id")
            if mail_id in seen_ids:
                continue
            if mail_id is not None:
                seen_ids.add(mail_id)
            link = _extract_activation_link(mail)
            if link:
                return link
        time.sleep(1)
    return None


def call_activation_api(activation_link: str, proxy: str = None) -> dict:
    link = str(activation_link or "").strip()
    if not link:
        return {"ok": False, "error": "激活链接为空"}

    parsed = urlparse(link)
    if not parsed.scheme or not parsed.netloc:
        return {"ok": False, "error": "激活链接格式无效"}

    params = parse_qs(parsed.query)
    request_params = {
        key: values[-1]
        for key, values in params.items()
        if values
    }
    try:
        response = curl_requests.get(
            link,
            impersonate="chrome124",
            timeout=20,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    payload = None
    try:
        payload = response.json()
    except Exception:
        payload = None

    if response.status_code >= 400:
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("detail") or payload.get("message") or "").strip()
        if not detail:
            detail = (response.text or "")[:200]
        return {"ok": False, "error": detail or f"HTTP {response.status_code}", "params": request_params}

    return {"ok": True, "data": payload if payload is not None else response.text, "params": request_params}


def obtain_qwen_oauth_tokens_with_login(
    executor: Any,
    *,
    email: str,
    password: str,
    web_token: str,
    log_fn: Optional[Callable[[str], None]] = None,
) -> dict:
    token = str(web_token or "").strip()
    if not token:
        raise RuntimeError("缺少 web token，无法补登 OAuth")
    if not email or not password:
        raise RuntimeError("缺少登录凭据，无法补登 OAuth")
    if callable(log_fn):
        log_fn("使用现有 web token 补登 OAuth")
    raise NotImplementedError("Qwen OAuth login bootstrap is not implemented yet")


def decode_qwen_token_payload(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = (-len(payload)) % 4
    if padding:
        payload += "=" * padding
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _default_full_name(email: str) -> str:
    local = str(email or "user").split("@", 1)[0]
    normalized = re.sub(r"[^a-zA-Z0-9]+", " ", local).strip()
    if not normalized:
        normalized = "Qwen User"
    parts = [part for part in normalized.split() if part]
    if not parts:
        return "Qwen User"
    return " ".join(part[:1].upper() + part[1:] for part in parts[:3])


def _list_mails_for_email(mailbox: Any, account_email: str) -> list[dict]:
    target_email = str(account_email or "").strip()
    get_mails = getattr(mailbox, "_get_mails", None)
    if callable(get_mails) and target_email:
        try:
            mails = get_mails(target_email)
            if isinstance(mails, list):
                return mails
        except Exception:
            pass

    account = MailboxAccount(email=target_email, account_id=target_email)
    list_methods = []
    for name in ("_list_mails", "list_mails"):
        method = getattr(mailbox, name, None)
        if callable(method):
            list_methods.append(method)
    for method in list_methods:
        for candidate in (target_email, account.account_id):
            if not candidate:
                continue
            try:
                mails = method(candidate)
                if isinstance(mails, list):
                    return mails
            except TypeError:
                try:
                    mails = method(account)
                    if isinstance(mails, list):
                        return mails
                except Exception:
                    continue
            except Exception:
                continue
    return []


def _extract_activation_link(mail: dict) -> Optional[str]:
    candidates = [
        mail.get("raw"),
        mail.get("html"),
        mail.get("body"),
        mail.get("content"),
        mail.get("subject"),
    ]
    for candidate in candidates:
        link = _find_activation_link_in_text(candidate)
        if link:
            return link
    return None


def _find_activation_link_in_text(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value)
    match = _ACTIVATION_URL_PATTERN.search(text)
    if match:
        return match.group(0)

    decoded_body = _decode_mail_body(text)
    match = _ACTIVATION_URL_PATTERN.search(decoded_body)
    if match:
        return match.group(0)

    unescaped = unescape(decoded_body)
    match = _ACTIVATION_URL_PATTERN.search(unescaped)
    if match:
        return match.group(0)
    return None


def _decode_mail_body(raw: str) -> str:
    text = str(raw or "")
    if not text:
        return ""

    lower_text = text.lower()
    is_base64_mail = "content-transfer-encoding: base64" in lower_text
    if "\r\n\r\n" in text:
        body = text.split("\r\n\r\n", 1)[1]
    elif "\n\n" in text:
        body = text.split("\n\n", 1)[1]
    else:
        body = text

    body = body.strip()
    if is_base64_mail:
        compact = re.sub(r"\s+", "", body)
        try:
            decoded = base64.b64decode(compact, validate=False)
            body = decoded.decode("utf-8", errors="ignore")
        except Exception:
            pass
    return body


def random_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.choice(alphabet) for _ in range(max(length, 8)))
