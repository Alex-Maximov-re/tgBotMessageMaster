"""Клиент официального API Авито: токен, профиль, чаты и вебхук."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("avito_notify.avito")

_API = "https://api.avito.ru"


class AvitoError(Exception):
    def __init__(self, status: int, body: str, method: str, path: str):
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        preview = " ".join(body.split())[:500]
        super().__init__(f"Авито {method} {path} → {status}: {preview}")


class AvitoClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.AsyncClient):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._token: str | None = None
        self._token_until = 0.0

    async def get_self(self) -> dict[str, Any]:
        payload = await self._request("GET", "/core/v1/accounts/self")
        return payload if isinstance(payload, dict) else {}

    async def get_chat(self, user_id: int, chat_id: str) -> dict[str, Any]:
        path = f"/messenger/v2/accounts/{user_id}/chats/{quote(str(chat_id), safe='')}"
        payload = await self._request("GET", path)
        return payload if isinstance(payload, dict) else {}

    async def unread_chats(self, user_id: int) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            f"/messenger/v2/accounts/{user_id}/chats",
            params={"unread_only": "true", "limit": 50},
        )
        return payload if isinstance(payload, dict) else {"chats": payload}

    async def subscribe(self, url: str) -> Any:
        return await self._request("POST", "/messenger/v3/webhook", json={"url": url})

    async def subscriptions(self) -> Any:
        return await self._request("POST", "/messenger/v1/subscriptions", json={})

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._send(method, path, retry_auth=True, **kwargs)
        if response.status_code >= 400:
            raise AvitoError(response.status_code, response.text, method, path)
        if not response.content:
            return {}
        return response.json()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        retry_auth: bool,
        **kwargs: Any,
    ) -> httpx.Response:
        token = await self._access_token(force=False)
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        response = await self._http.request(method, _API + path, headers=headers, **kwargs)
        if response.status_code == 401 and retry_auth:
            log.info("Токен Авито отклонён, запрашиваю новый")
            token = await self._access_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            response = await self._http.request(
                method, _API + path, headers=headers, **kwargs
            )
        return response

    async def _access_token(self, *, force: bool) -> str:
        if not force and self._token and time.monotonic() < self._token_until:
            return self._token
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        last: httpx.Response | None = None
        for path in ("/token", "/token/"):
            response = await self._http.post(_API + path, data=form)
            last = response
            if response.status_code == 404:
                continue
            if response.status_code >= 400:
                raise AvitoError(response.status_code, response.text, "POST", path)
            payload = response.json()
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise AvitoError(response.status_code, response.text, "POST", path)
            expires = 3600
            if isinstance(payload, dict):
                try:
                    expires = int(payload.get("expires_in") or 3600)
                except (TypeError, ValueError):
                    expires = 3600
            self._token = token
            self._token_until = time.monotonic() + max(30, expires - 120)
            return token
        status = last.status_code if last is not None else 0
        body = last.text if last is not None else ""
        raise AvitoError(status, body or "token endpoint not found", "POST", "/token")
