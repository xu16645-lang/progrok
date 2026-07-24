from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent.parent
ACCOUNTS_FILE = APP_DIR / "runtime" / "data" / "hotmail_accounts.json"
DEFAULT_HELPER_URL = "http://127.0.0.1:17373"
_lock = threading.RLock()
_reservations: set[str] = set()


def _load() -> list[dict[str, Any]]:
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _save(accounts: list[dict[str, Any]]) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, ACCOUNTS_FILE)


def parse_import_text(text: str) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    valid: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    for line_no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("----")]
        if len(parts) != 4:
            errors.append({"line": line_no, "error": "格式应为 email----password----client-id----refresh-token"})
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email or not client_id or not refresh_token:
            errors.append({"line": line_no, "error": "邮箱、客户端 ID、刷新令牌不能为空"})
            continue
        valid.append({
            "email": email.lower(),
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh_token,
        })
    return valid, errors


def import_accounts(text: str) -> dict[str, Any]:
    parsed, errors = parse_import_text(text)
    added = updated = 0
    account_ids: list[str] = []
    with _lock:
        accounts = _load()
        by_email = {str(item.get("email") or "").lower(): item for item in accounts}
        for row in parsed:
            existing = by_email.get(row["email"])
            if existing:
                existing.update(row)
                existing["mail_healthy"] = None
                existing["mail_health_error"] = ""
                existing["mail_checked_at"] = None
                account_ids.append(str(existing.get("id") or ""))
                updated += 1
            else:
                item: dict[str, Any] = {
                    "id": f"hm_{uuid.uuid4().hex[:16]}",
                    **row,
                    "used": False,
                    "failed": False,
                    "failure_reason": "",
                    "failed_at": None,
                    "created_at": time.time(),
                    "last_used_at": None,
                    "mail_healthy": None,
                    "mail_health_error": "",
                    "mail_checked_at": None,
                }
                accounts.append(item)
                by_email[row["email"]] = item
                account_ids.append(str(item["id"]))
                added += 1
        if parsed:
            _save(accounts)
    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "invalid": len(errors),
        "errors": errors,
        "account_ids": [account_id for account_id in account_ids if account_id],
    }


def _masked(value: str, *, keep: int = 4) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def list_accounts() -> dict[str, Any]:
    with _lock:
        accounts = _load()
        rows = [{
            "id": item.get("id"),
            "email": item.get("email"),
            "used": bool(item.get("used")),
            "failed": bool(item.get("failed")),
            "failure_reason": str(item.get("failure_reason") or ""),
            "failed_at": item.get("failed_at"),
            "reserved": str(item.get("id") or "") in _reservations,
            "has_password": bool(item.get("password")),
            "client_id_masked": _masked(str(item.get("client_id") or "")),
            "refresh_token_masked": _masked(str(item.get("refresh_token") or "")),
            "created_at": item.get("created_at"),
            "last_used_at": item.get("last_used_at"),
            "mail_healthy": item.get("mail_healthy") if isinstance(item.get("mail_healthy"), bool) else None,
            "mail_health_error": str(item.get("mail_health_error") or ""),
            "mail_checked_at": item.get("mail_checked_at"),
        } for item in accounts]
    return {
        "ok": True,
        "accounts": rows,
        "total": len(rows),
        "available": sum(1 for item in rows if not item["used"] and not item["failed"] and not item["reserved"] and item["mail_healthy"] is not False),
        "used": sum(1 for item in rows if item["used"]),
        "failed": sum(1 for item in rows if item["failed"]),
        "healthy": sum(1 for item in rows if item["mail_healthy"] is True),
        "unhealthy": sum(1 for item in rows if item["mail_healthy"] is False),
        "unchecked": sum(1 for item in rows if item["mail_healthy"] is None),
    }


def reserve_account() -> dict[str, Any]:
    with _lock:
        for item in _load():
            account_id = str(item.get("id") or "")
            if not account_id or item.get("used") or item.get("failed") or item.get("mail_healthy") is False or account_id in _reservations:
                continue
            if not item.get("email") or not item.get("client_id") or not item.get("refresh_token"):
                continue
            _reservations.add(account_id)
            return dict(item)
    raise RuntimeError("微软邮箱账户池没有可用账号，请先导入或恢复未用账号")


def release_account(account_id: str) -> None:
    with _lock:
        _reservations.discard(str(account_id or ""))


def mark_used(account_id: str) -> bool:
    account_id = str(account_id or "")
    with _lock:
        accounts = _load()
        found = False
        for item in accounts:
            if str(item.get("id") or "") == account_id:
                item["used"] = True
                item["failed"] = False
                item["failure_reason"] = ""
                item["failed_at"] = None
                item["last_used_at"] = time.time()
                found = True
                break
        _reservations.discard(account_id)
        if found:
            _save(accounts)
        return found


def mark_failed(account_id: str, reason: str = "") -> bool:
    account_id = str(account_id or "")
    with _lock:
        accounts = _load()
        found = False
        for item in accounts:
            if str(item.get("id") or "") == account_id:
                item["used"] = False
                item["failed"] = True
                item["failure_reason"] = str(reason or "注册失败")[:500]
                item["failed_at"] = time.time()
                found = True
                break
        _reservations.discard(account_id)
        if found:
            _save(accounts)
        return found


def update_refresh_token(account_id: str, refresh_token: str) -> bool:
    account_id = str(account_id or "")
    refresh_token = str(refresh_token or "").strip()
    if not account_id or not refresh_token:
        return False
    with _lock:
        accounts = _load()
        found = False
        for item in accounts:
            if str(item.get("id") or "") == account_id:
                item["refresh_token"] = refresh_token
                found = True
                break
        if found:
            _save(accounts)
        return found


def set_used(account_id: str, used: bool) -> bool:
    account_id = str(account_id or "")
    with _lock:
        accounts = _load()
        found = False
        for item in accounts:
            if str(item.get("id") or "") == account_id:
                item["used"] = bool(used)
                if used:
                    item["failed"] = False
                    item["failure_reason"] = ""
                    item["failed_at"] = None
                else:
                    item["last_used_at"] = None
                    item["failed"] = False
                    item["failure_reason"] = ""
                    item["failed_at"] = None
                found = True
                break
        if not used:
            _reservations.discard(account_id)
        if found:
            _save(accounts)
        return found


def delete_account(account_id: str) -> bool:
    account_id = str(account_id or "")
    with _lock:
        accounts = _load()
        kept = [item for item in accounts if str(item.get("id") or "") != account_id]
        if len(kept) == len(accounts):
            return False
        _reservations.discard(account_id)
        _save(kept)
        return True


def delete_used_accounts() -> dict[str, int]:
    """Delete all used accounts that are not reserved by a running task."""
    with _lock:
        accounts = _load()
        kept: list[dict[str, Any]] = []
        deleted = 0
        skipped_reserved = 0
        for item in accounts:
            account_id = str(item.get("id") or "")
            if item.get("used") and account_id not in _reservations:
                deleted += 1
                continue
            if item.get("used") and account_id in _reservations:
                skipped_reserved += 1
            kept.append(item)
        if deleted:
            _save(kept)
        return {"deleted": deleted, "skipped_reserved": skipped_reserved}


def delete_unhealthy_accounts() -> dict[str, int]:
    """Delete accounts whose latest mailbox probe failed."""
    with _lock:
        accounts = _load()
        kept: list[dict[str, Any]] = []
        deleted = 0
        skipped_reserved = 0
        for item in accounts:
            account_id = str(item.get("id") or "")
            unhealthy = item.get("mail_healthy") is False
            if unhealthy and account_id not in _reservations:
                deleted += 1
                continue
            if unhealthy and account_id in _reservations:
                skipped_reserved += 1
            kept.append(item)
        if deleted:
            _save(kept)
        return {"deleted": deleted, "skipped_reserved": skipped_reserved}


def _request_json(base_url: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 12) -> dict[str, Any]:
    url = f"{str(base_url or DEFAULT_HELPER_URL).rstrip('/')}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401 or any(
            marker in detail.lower()
            for marker in ("invalid_grant", "aadsts70000", "unauthorized")
        ):
            raise RuntimeError(f"微软邮箱凭证失效：{detail[:260]}") from exc
        raise RuntimeError(f"本地邮箱助手 HTTP {exc.code}: {detail[:200]}") from exc
    except (URLError, OSError) as exc:
        raise RuntimeError(f"无法连接本地邮箱助手 {url}: {exc}") from exc
    if not isinstance(body, dict):
        raise RuntimeError("本地邮箱助手返回了无效 JSON")
    return body


def helper_health(base_url: str | None = None) -> dict[str, Any]:
    result = _request_json(base_url or DEFAULT_HELPER_URL, "/health", timeout=5)
    return {"ok": bool(result.get("ok")), "url": str(base_url or DEFAULT_HELPER_URL).rstrip("/"), **result}


def probe_account(account_id: str, base_url: str | None = None) -> dict[str, Any]:
    account_id = str(account_id or "")
    with _lock:
        account = next((dict(item) for item in _load() if str(item.get("id") or "") == account_id), None)
    if not account:
        return {"ok": False, "account_id": account_id, "error": "邮箱账号不存在"}
    checked_at = time.time()
    error = ""
    result: dict[str, Any] = {}
    try:
        result = _request_json(
            base_url or DEFAULT_HELPER_URL,
            "/messages",
            {
                "email": account.get("email"),
                "clientId": account.get("client_id"),
                "refreshToken": account.get("refresh_token"),
                "mailboxes": ["INBOX"],
                "top": 1,
            },
            timeout=45,
        )
        if not result.get("ok"):
            error = str(result.get("error") or result.get("message") or "邮箱助手测活失败")
    except Exception as exc:
        error = str(exc)
    healthy = not error
    with _lock:
        accounts = _load()
        for item in accounts:
            if str(item.get("id") or "") != account_id:
                continue
            next_token = str(result.get("nextRefreshToken") or "").strip()
            if next_token:
                item["refresh_token"] = next_token
            item["mail_healthy"] = healthy
            item["mail_health_error"] = error[:500]
            item["mail_checked_at"] = checked_at
            _save(accounts)
            break
    return {
        "ok": healthy,
        "account_id": account_id,
        "email": str(account.get("email") or ""),
        "error": error or None,
        "transport": str(result.get("transport") or ""),
        "checked_at": checked_at,
    }


def probe_accounts(base_url: str | None = None, account_ids: list[str] | None = None) -> dict[str, Any]:
    if account_ids is None:
        with _lock:
            account_ids = [str(item.get("id") or "") for item in _load() if item.get("id")]
    else:
        account_ids = [str(account_id or "") for account_id in account_ids if str(account_id or "")]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(account_ids)))) as pool:
        futures = [pool.submit(probe_account, account_id, base_url) for account_id in account_ids]
        for future in as_completed(futures):
            results.append(future.result())
    passed = sum(1 for item in results if item.get("ok"))
    return {
        "ok": passed == len(results),
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


class HotmailLocalReceiver:
    def __init__(
        self,
        account: dict[str, Any],
        base_url: str | None = None,
        *,
        verification_target: str = "chatgpt",
    ):
        self.account_id = str(account.get("id") or "")
        self.email = str(account.get("email") or "")
        self.password = str(account.get("password") or "")
        self.client_id = str(account.get("client_id") or "")
        self.refresh_token = str(account.get("refresh_token") or "")
        self.base_url = str(base_url or DEFAULT_HELPER_URL).rstrip("/")
        self.verification_target = (
            "xai" if str(verification_target or "").strip().lower() == "xai" else "chatgpt"
        )
        self.created_at_ms = int(time.time() * 1000)

    def mark_code_request_started(self) -> None:
        """Ignore messages from before the current verification-code request."""
        # Small clock-skew allowance for provider timestamps.
        self.created_at_ms = int(time.time() * 1000) - 5000

    def wait_for_code(self, timeout: float = 120, *, should_cancel=None, poll_interval: float | None = None,
                      exclude_codes: set[str] | None = None) -> str:
        deadline = time.time() + float(timeout or 120)
        poll = max(1.0, min(float(poll_interval or 2.5), 4.0))
        last_error = ""
        is_xai = self.verification_target == "xai"
        excluded = {str(code or "").strip() for code in (exclude_codes or set()) if str(code or "").strip()}
        normalized_excluded = {
            re.sub(r"[^A-Za-z0-9]", "", code).upper() for code in excluded
        }
        helper_excluded = set(excluded)
        if is_xai:
            for code in normalized_excluded:
                if len(code) == 6:
                    helper_excluded.add(f"{code[:3]}-{code[3:]}")
        if is_xai:
            sender_filters = ["x.ai", "xai", "grok"]
            subject_filters = ["xai", "grok", "verification"]
            required_keywords = ["x.ai", "xai", "grok"]
            code_patterns = [
                {"source": r"\b([A-Z0-9]{3})-([A-Z0-9]{3})\b", "flags": "i"},
                {
                    "source": r"(?:verification\s*code|code)[^A-Z0-9]{0,24}([A-Z0-9]{6})\b",
                    "flags": "i",
                },
                {"source": r"\b([A-Z0-9]{6})\b", "flags": "i"},
            ]
        else:
            sender_filters = []
            subject_filters = []
            required_keywords = ["openai", "chatgpt", "verification", "code"]
            code_patterns = []
        while time.time() < deadline:
            if callable(should_cancel) and should_cancel():
                raise RuntimeError("cancelled while waiting for email code")
            payload = {
                "email": self.email,
                "clientId": self.client_id,
                "refreshToken": self.refresh_token,
                "mailboxes": ["INBOX"],
                "top": 10,
                "senderFilters": sender_filters,
                "subjectFilters": subject_filters,
                "excludeCodes": sorted(helper_excluded),
                "filterAfterTimestamp": self.created_at_ms,
                "allowTimeFallback": False,
                "requiredKeywords": required_keywords,
                "codePatterns": code_patterns,
            }
            try:
                result = _request_json(self.base_url, "/code", payload, timeout=min(35, max(8, deadline - time.time())))
                next_token = str(result.get("nextRefreshToken") or "").strip()
                if next_token:
                    self.refresh_token = next_token
                    update_refresh_token(self.account_id, next_token)
                code = str(result.get("code") or "").strip()
                normalized = re.sub(r"[^A-Za-z0-9]", "", code).upper()
                if is_xai:
                    valid = bool(re.fullmatch(r"[A-Z0-9]{6}", normalized))
                    if result.get("ok") and valid and normalized not in normalized_excluded:
                        return normalized
                elif result.get("ok") and re.fullmatch(r"\d{6}", code) and code not in excluded:
                    return code
                last_error = str(result.get("error") or result.get("message") or "")
            except Exception as exc:
                last_error = str(exc)
            slept = 0.0
            while slept < poll and time.time() < deadline:
                if callable(should_cancel) and should_cancel():
                    raise RuntimeError("cancelled while waiting for email code")
                step = min(0.25, poll - slept)
                time.sleep(step)
                slept += step
        suffix = f": {last_error}" if last_error else ""
        raise RuntimeError(f"等待微软邮箱验证码超时{suffix}")


def create_receiver(
    base_url: str | None = None,
    *,
    verification_target: str = "chatgpt",
) -> tuple[str, HotmailLocalReceiver]:
    account = reserve_account()
    receiver = HotmailLocalReceiver(
        account,
        base_url,
        verification_target=verification_target,
    )
    return receiver.email, receiver
