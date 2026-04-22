from __future__ import annotations

import json
import sys
import types
from typing import Any, Optional

try:
    from curl_cffi import requests as curl_requests
except ModuleNotFoundError:
    import requests as curl_requests

    curl_cffi_module = types.ModuleType("curl_cffi")
    curl_cffi_module.requests = curl_requests
    sys.modules.setdefault("curl_cffi", curl_cffi_module)

import core.base_mailbox as mailbox_module
from core.base_mailbox import BaseMailbox
from core.base_platform import Account, BasePlatform, RegisterConfig
from core.registry import register
import platforms.qwen.core as qwen_core
import platforms.qwen.cpa_upload as qwen_cpa_upload


@register
class QwenPlatform(BasePlatform):
    name = "qwen"
    display_name = "Qwen"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]

    def __init__(
        self,
        config: Optional[RegisterConfig] = None,
        mailbox: Optional[BaseMailbox] = None,
    ):
        super().__init__(config or RegisterConfig())
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        password = password or qwen_core.random_password()
        log = getattr(self, "_log_fn", print)

        with self._make_executor() as executor:
            result = qwen_core.QwenRegister(executor=executor, log_fn=log).register(
                email=email,
                password=password,
                full_name=self._resolve_full_name(email),
            )

        if result.get("status") != "success":
            raise RuntimeError(str(result.get("error") or "Qwen 注册失败"))

        tokens = result.get("tokens")
        if not isinstance(tokens, dict):
            tokens = {}
        web_token = str(tokens.get("cookie:token") or "").strip()
        if not web_token:
            raise RuntimeError("Qwen 注册成功但未返回 token")

        extra = {
            "raw_tokens": dict(tokens),
        }
        extra.update(self._extract_oauth_fields(tokens))

        return Account(
            platform=self.name,
            email=str(result.get("email") or email),
            password=str(result.get("password") or password),
            token=web_token,
            extra=extra,
        )

    def check_valid(self, account: Account) -> bool:
        return bool(str(getattr(account, "token", "") or "").strip())

    def get_platform_actions(self) -> list:
        return [
            {"id": "activate_account", "label": "激活账号", "params": []},
            {"id": "get_user_info", "label": "获取用户信息", "params": []},
            {
                "id": "upload_cpa",
                "label": "上传 CPA",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API Key", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "activate_account":
            return self._activate_account(account)
        if action_id == "get_user_info":
            return self._get_user_info(account)
        if action_id == "upload_cpa":
            return self._upload_cpa(account, params or {})
        raise NotImplementedError(f"未知操作: {action_id}")

    def _activate_account(self, account: Account) -> dict:
        mailbox = self.mailbox or self._build_action_mailbox()
        if mailbox is None:
            return {"ok": False, "error": "未配置邮箱服务，无法查找激活邮件"}

        timeout = self.get_mailbox_otp_timeout()
        link = qwen_core.wait_for_activation_link(mailbox, account_email=account.email, timeout=timeout)
        if not link:
            return {"ok": False, "error": f"在 {timeout}s 内未找到激活邮件"}

        result = qwen_core.call_activation_api(link, proxy=self.config.proxy if self.config else None)
        if result.get("ok"):
            return {"ok": True, "data": result.get("data") or {"activation_link": link}}
        return {"ok": False, "error": result.get("error") or "激活失败"}

    def _get_user_info(self, account: Account) -> dict:
        token = str(account.token or "").strip()
        if not token:
            return {"ok": False, "error": "账号缺少 token"}

        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0",
        }
        request_kwargs = {
            "headers": headers,
            "timeout": 20,
            "impersonate": "chrome124",
        }
        if self.config and self.config.proxy:
            request_kwargs["proxies"] = {"http": self.config.proxy, "https": self.config.proxy}

        primary_endpoints = [
            "https://chat.qwen.ai/api/v1/users/me",
            "https://chat.qwen.ai/api/v1/auths/",
        ]
        for endpoint in primary_endpoints:
            try:
                response = curl_requests.get(endpoint, **request_kwargs)
            except Exception:
                continue
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:
                    data = {"raw": response.text}
                return {"ok": True, "data": data}

        fallback_endpoint = "https://chat.qwen.ai/api/v1/chats/"
        try:
            fallback_response = curl_requests.get(fallback_endpoint, **request_kwargs)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if fallback_response.status_code != 200:
            return {"ok": False, "error": (fallback_response.text or "")[:200] or f"HTTP {fallback_response.status_code}"}

        try:
            chats = fallback_response.json()
        except Exception:
            chats = []
        if not isinstance(chats, list):
            chats = []
        payload = qwen_core.decode_qwen_token_payload(token)
        user_id = str(payload.get("id") or payload.get("user_id") or payload.get("sub") or "").strip()
        return {
            "ok": True,
            "data": {
                "来源": "会话列表接口",
                "会话数量": len(chats),
                "用户ID": user_id,
            },
        }

    def _upload_cpa(self, account: Account, params: dict) -> dict:
        oauth_info = self._resolve_oauth_info(account)
        extra_patch = {}
        if not oauth_info.get("refresh_token"):
            try:
                with self._make_executor() as executor:
                    oauth_info = qwen_core.obtain_qwen_oauth_tokens_with_login(
                        executor,
                        email=account.email,
                        password=account.password,
                        web_token=account.token,
                        log_fn=getattr(self, "_log_fn", print),
                    )
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
            extra_patch = {
                "oauth_access_token": oauth_info.get("oauth_access_token", ""),
                "refresh_token": oauth_info.get("refresh_token", ""),
                "resource_url": oauth_info.get("resource_url", ""),
            }

        account_like = type("QwenCPAAccount", (), {})()
        account_like.email = account.email
        account_like.access_token = oauth_info.get("oauth_access_token") or account.token
        account_like.refresh_token = oauth_info.get("refresh_token", "")
        account_like.resource_url = oauth_info.get("resource_url", "")
        token_json = qwen_cpa_upload.generate_token_json(account_like)
        upload_kwargs = {
            "api_url": params.get("api_url"),
            "api_key": params.get("api_key"),
        }
        if self.config and self.config.proxy:
            upload_kwargs["proxy"] = self.config.proxy
        ok, message = qwen_cpa_upload.upload_to_cpa(token_json, **upload_kwargs)
        result = {"ok": ok}
        if ok:
            result["data"] = message
        else:
            result["error"] = message
        if extra_patch and ok:
            result["account_extra_patch"] = extra_patch
        elif extra_patch:
            result["account_extra_patch"] = extra_patch
        return result

    def _build_action_mailbox(self) -> Optional[BaseMailbox]:
        extra = (self.config.extra or {}) if self.config else {}
        provider = str(extra.get("mail_provider") or "").strip()
        if not provider:
            return None
        mailbox = mailbox_module.create_mailbox(provider, extra=extra, proxy=self.config.proxy if self.config else None)
        mailbox._task_control = getattr(self, "_task_control", None)
        return mailbox

    def _resolve_oauth_info(self, account: Account) -> dict[str, str]:
        extra = account.extra or {}
        resolved = {
            "oauth_access_token": str(extra.get("oauth_access_token") or "").strip(),
            "refresh_token": str(extra.get("refresh_token") or "").strip(),
            "resource_url": str(extra.get("resource_url") or "").strip(),
        }
        raw_tokens = extra.get("raw_tokens")
        if isinstance(raw_tokens, dict):
            resolved.update({k: v for k, v in self._extract_oauth_fields(raw_tokens).items() if v})
        return resolved

    def _extract_oauth_fields(self, tokens: dict[str, Any]) -> dict[str, str]:
        payload_raw = tokens.get("oauth_payload") if isinstance(tokens, dict) else None
        if not payload_raw:
            return {}
        if isinstance(payload_raw, str):
            try:
                payload = json.loads(payload_raw)
            except Exception:
                return {}
        elif isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            return {}
        return {
            "oauth_access_token": str(payload.get("oauth_access_token") or payload.get("access_token") or "").strip(),
            "refresh_token": str(payload.get("refreshToken") or payload.get("refresh_token") or "").strip(),
            "resource_url": str(payload.get("resource_url") or payload.get("resourceUrl") or "").strip(),
        }

    def _resolve_full_name(self, email: str) -> str:
        local = str(email or "user").split("@", 1)[0]
        parts = [segment for segment in local.replace(".", " ").replace("_", " ").split() if segment]
        if not parts:
            return "Qwen User"
        return " ".join(part[:1].upper() + part[1:] for part in parts[:3])
