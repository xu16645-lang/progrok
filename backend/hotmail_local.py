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
# Microsoft plus-addressing: local+tag@domain lands in the same mailbox.
# Each imported base account can register ALIAS_USES times:
#   base@domain, base+1@domain, ..., base+(ALIAS_USES-1)@domain
ALIAS_USES = 10
_lock = threading.RLock()
# account_id -> currently reserved plus-alias indices for in-flight tasks
_reservations: dict[str, set[int]] = {}
# account_id -> alias_index -> latest in-flight verification state. Codes are
# intentionally process-local and never written to the credential file.
_verification_states: dict[str, dict[int, dict[str, Any]]] = {}
# Round-robin cursor so concurrent tasks spread across physical mailboxes first.
_reserve_cursor = 0
# Serialize token refresh / rotation per physical mailbox (account_id).
_token_locks_guard = threading.Lock()
_token_locks: dict[str, threading.RLock] = {}


def _normalize_index_set(raw: Any) -> set[int]:
    values: set[int] = set()
    if not isinstance(raw, list):
        return values
    for value in raw:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < ALIAS_USES:
            values.add(index)
    return values


def _normalize_used_aliases(item: dict[str, Any]) -> set[int]:
    """Return successful plus-alias indices already consumed by this mailbox.

    If used_aliases is explicitly present (even as []), trust it. Only fall back
    to legacy used/use_count inference when the field is missing entirely.
    """
    if "used_aliases" in item:
        return _normalize_index_set(item.get("used_aliases"))
    # Legacy rows without used_aliases field.
    try:
        count = int(item.get("use_count")) if item.get("use_count") is not None else None
    except (TypeError, ValueError):
        count = None
    if count is not None and count > 0:
        return set(range(min(ALIAS_USES, max(0, count))))
    if item.get("used"):
        return set(range(ALIAS_USES))
    return set()


def _normalize_failed_aliases(item: dict[str, Any]) -> set[int]:
    """Return alias indices that failed registration (not account-level auth failure)."""
    return _normalize_index_set(item.get("failed_aliases"))


def _account_level_failed(item: dict[str, Any]) -> bool:
    """True only for registration failures, never mailbox probe failures."""
    if item.get("account_failed") is True:
        return True
    # Legacy rows before account_failed existed: failed=True and no alias metadata.
    if "account_failed" not in item and item.get("failed") and not _normalize_failed_aliases(item):
        return True
    return False


def _coerce_alias_index(value: Any) -> int | None:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= index < ALIAS_USES:
        return index
    return None


def _prefer_mail_healthy(left: Any, right: Any) -> bool | None:
    if left is False or right is False:
        return False
    if left is True or right is True:
        return True
    return None


def _row_account_failed(item: dict[str, Any]) -> bool:
    """Whether a stored row itself is account-quarantined (ignores mail_healthy)."""
    if item.get("account_failed") is True:
        return True
    if "account_failed" not in item and item.get("failed") and not _normalize_failed_aliases(item):
        return True
    return False


def _plus_alias_offset(email: str) -> int | None:
    """Map a stored plus-address identity to base-mailbox slot offset.

    Returns:
      0 for plain base addresses
      N for local+N@domain when 1 <= N < ALIAS_USES
      None when the plus tag cannot be mapped safely
    """
    value = str(email or "").strip().lower()
    if "@" not in value:
        return 0
    local, _domain = value.rsplit("@", 1)
    if "+" not in local:
        return 0
    tag = local.split("+", 1)[1].strip()
    if not tag.isdigit():
        return None
    index = int(tag)
    if index == 0:
        return 0
    if 1 <= index < ALIAS_USES:
        return index
    return None


def _local_alias_to_base_slot(local_index: int, original_email: str) -> int | None:
    """Map a row-local alias index to the base-mailbox slot using build_plus_alias rules.

    For stored email base+N@domain:
      index 0 -> base+N  (slot N)  because index 0 returns the stored address as-is
      index 1 -> base+1  (slot 1)  because index>0 strips the tag then uses +index
      index k -> base+k  (slot k)  for k >= 1
    For plain base@domain the mapping is identity.
    """
    try:
        index = int(local_index)
    except (TypeError, ValueError):
        return None
    if index < 0 or index >= ALIAS_USES:
        return None
    offset = _plus_alias_offset(original_email)
    if offset is None:
        return None
    if index <= 0:
        return offset
    # index >= 1: build_plus_alias strips any +tag and emits base+index.
    return index


def _set_verification_state(
    account_id: str,
    alias_index: int,
    email: str,
    status: str,
    *,
    code: str = "",
    error: str = "",
) -> None:
    account_key = str(account_id or "")
    index = _coerce_alias_index(alias_index)
    if not account_key or index is None:
        return
    with _lock:
        entries = _verification_states.setdefault(account_key, {})
        entries[index] = {
            "alias_index": index,
            "email": str(email or "").strip().lower(),
            "status": str(status or "waiting"),
            "code": str(code or "")[:16],
            "error": str(error or "")[:200],
            "updated_at": time.time(),
        }


def _verification_entries(account_id: str) -> list[dict[str, Any]]:
    entries = _verification_states.get(str(account_id or ""), {})
    return sorted(
        (dict(item) for item in entries.values()),
        key=lambda item: float(item.get("updated_at") or 0),
        reverse=True,
    )


def _map_alias_indices_to_base(indices: set[int], original_email: str) -> set[int] | None:
    """Translate a set of row-local indices; None means unmappable."""
    mapped: set[int] = set()
    for index in indices:
        slot = _local_alias_to_base_slot(index, original_email)
        if slot is None:
            return None
        mapped.add(slot)
    return mapped


def _aliases_in_base_space(
    item: dict[str, Any],
    original_email: str,
) -> tuple[set[int], set[int], bool]:
    """Translate row-local alias indices into base-mailbox indices.

    Historical dual rows treated each imported address as its own "base".
    Mapping follows build_plus_alias() so base+1 local [0,1,2] becomes {1,2},
    not {1,2,3}. Unmappable plus tags force a conservative full exhaust.
    """
    offset = _plus_alias_offset(original_email)
    if offset is None:
        return set(range(ALIAS_USES)), set(), True
    # Fully exhausted legacy row under a plus identity cannot be remapped precisely.
    if (
        offset > 0
        and item.get("used")
        and "used_aliases" not in item
        and "failed_aliases" not in item
    ):
        return set(range(ALIAS_USES)), set(), True
    used = _map_alias_indices_to_base(_normalize_used_aliases(item), original_email)
    failed = _map_alias_indices_to_base(_normalize_failed_aliases(item), original_email)
    if used is None or failed is None:
        return set(range(ALIAS_USES)), set(), True
    return used, failed - used, False


def _apply_alias_sets(item: dict[str, Any], used: set[int], failed: set[int]) -> None:
    used_set = {index for index in used if 0 <= index < ALIAS_USES}
    failed_set = {index for index in failed if 0 <= index < ALIAS_USES} - used_set
    item["used_aliases"] = sorted(used_set)
    item["failed_aliases"] = sorted(failed_set)
    item["use_count"] = len(used_set)
    item["used"] = len(used_set) >= ALIAS_USES


def _account_token_lock(account_id: str) -> threading.RLock:
    key = str(account_id or "")
    with _token_locks_guard:
        lock = _token_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _token_locks[key] = lock
        return lock


def _drop_token_lock(account_id: str) -> None:
    key = str(account_id or "")
    if not key:
        return
    with _token_locks_guard:
        _token_locks.pop(key, None)


def _code_fetch_top(account_id: str) -> int:
    """How many recent messages to fetch for verification codes.

    Helper caps at 30. Use the full window so 10 concurrent plus-aliases plus
    resends / unrelated mail do not push codes out of range.
    """
    _ = account_id  # reserved for future dynamic sizing if helper limit grows
    return 30


def _merge_duplicate_base_accounts(
    accounts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Collapse historical base / base+N rows that share one physical mailbox."""
    merged: list[dict[str, Any]] = []
    by_base: dict[str, dict[str, Any]] = {}
    changed = False
    for item in accounts:
        row = dict(item)
        email = str(row.get("email") or "").strip().lower()
        base_email = base_mailbox_email(email)
        if not base_email or "@" not in base_email:
            merged.append(row)
            continue
        used, failed, unmappable = _aliases_in_base_space(row, email)
        if email != base_email or unmappable or used != _normalize_used_aliases(row) or failed != _normalize_failed_aliases(row):
            row["email"] = base_email
            _apply_alias_sets(row, used, failed)
            changed = True
        else:
            row["email"] = base_email
        existing = by_base.get(base_email)
        if existing is None:
            by_base[base_email] = row
            merged.append(row)
            continue
        changed = True
        existing_used = _normalize_used_aliases(existing)
        existing_failed = _normalize_failed_aliases(existing)
        merged_used = existing_used | used
        merged_failed = (existing_failed | failed) - merged_used
        if unmappable or len(merged_used) >= ALIAS_USES:
            merged_used = set(range(ALIAS_USES))
            merged_failed = set()
        for key in ("password", "client_id", "refresh_token"):
            if row.get(key):
                existing[key] = row[key]
        existing["email"] = base_email
        _apply_alias_sets(existing, merged_used, merged_failed)
        if _row_account_failed(existing) or _row_account_failed(row):
            existing["account_failed"] = True
            existing["failed"] = True
            reason = str(row.get("failure_reason") or existing.get("failure_reason") or "")
            if reason:
                existing["failure_reason"] = reason
            failed_at = row.get("failed_at") or existing.get("failed_at")
            if failed_at is not None:
                existing["failed_at"] = failed_at
        healthy = _prefer_mail_healthy(existing.get("mail_healthy"), row.get("mail_healthy"))
        existing["mail_healthy"] = healthy
        if healthy is False:
            err = str(row.get("mail_health_error") or existing.get("mail_health_error") or "")
            if err:
                existing["mail_health_error"] = err
        left_used = float(existing.get("last_used_at") or 0)
        right_used = float(row.get("last_used_at") or 0)
        if left_used or right_used:
            existing["last_used_at"] = max(left_used, right_used)
        # Drop the duplicate row; keep the first id for reservation continuity.
    return merged, changed


def _normalize_use_count(item: dict[str, Any]) -> int:
    return len(_normalize_used_aliases(item))


def _remaining_uses(item: dict[str, Any]) -> int:
    if _account_level_failed(item) or item.get("mail_healthy") is False:
        return 0
    taken = _normalize_used_aliases(item) | _normalize_failed_aliases(item)
    return max(0, ALIAS_USES - len(taken))


def _is_fully_exhausted(item: dict[str, Any]) -> bool:
    """True when every plus-alias slot is used or alias-failed (not account quarantine)."""
    if _account_level_failed(item) or item.get("mail_healthy") is False:
        return False
    return _remaining_uses(item) <= 0


def _sync_used_flag(item: dict[str, Any]) -> None:
    item["used"] = _is_fully_exhausted(item)


def _reserved_indices(account_id: str) -> set[int]:
    return set(_reservations.get(str(account_id or ""), set()))


def _free_alias_index(item: dict[str, Any], account_id: str) -> int | None:
    """Next free plus-alias index for this base mailbox, or None if exhausted."""
    if _account_level_failed(item) or item.get("mail_healthy") is False:
        return None
    taken = (
        _normalize_used_aliases(item)
        | _normalize_failed_aliases(item)
        | _reserved_indices(account_id)
    )
    for index in range(ALIAS_USES):
        if index not in taken:
            return index
    return None


def _is_account_available(item: dict[str, Any], *, account_id: str | None = None) -> bool:
    account_id = str(account_id or item.get("id") or "")
    if not account_id:
        return False
    if not item.get("email") or not item.get("client_id") or not item.get("refresh_token"):
        return False
    return _free_alias_index(item, account_id) is not None


def build_plus_alias(email: str, use_index: int) -> str:
    """Build registration address for the N-th use of a base mailbox.

    use_index 0 -> base email
    use_index 1 -> local+1@domain
    use_index k -> local+k@domain
    """
    value = str(email or "").strip().lower()
    if "@" not in value:
        raise ValueError(f"invalid email for plus alias: {email!r}")
    index = max(0, int(use_index or 0))
    if index <= 0:
        return value
    local, domain = value.rsplit("@", 1)
    # Strip an existing +tag so imported aliases still expand from the base local-part.
    base_local = local.split("+", 1)[0].strip()
    if not base_local or not domain:
        raise ValueError(f"invalid email for plus alias: {email!r}")
    return f"{base_local}+{index}@{domain}"


def base_mailbox_email(email: str) -> str:
    """Return the underlying mailbox address without a plus tag."""
    value = str(email or "").strip().lower()
    if "@" not in value:
        return value
    local, domain = value.rsplit("@", 1)
    base_local = local.split("+", 1)[0].strip()
    return f"{base_local}@{domain}" if base_local and domain else value


def _load() -> list[dict[str, Any]]:
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    accounts = [dict(item) for item in data if isinstance(item, dict)]
    merged, changed = _merge_duplicate_base_accounts(accounts)
    if changed:
        # Persist one-time base-mailbox collapse for historical dual rows.
        try:
            _save(merged)
        except Exception:
            pass
    return merged


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
        base_email = base_mailbox_email(email)
        valid.append({
            "email": base_email,
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
        # Always key physical mailboxes by base address so base+1 imports merge.
        by_email = {
            base_mailbox_email(str(item.get("email") or "")): item for item in accounts
        }
        for row in parsed:
            base_email = base_mailbox_email(row["email"])
            existing = by_email.get(base_email)
            if existing:
                existing.update(row)
                existing["email"] = base_email
                # Fresh credentials should un-quarantine a previously failed mailbox.
                existing["account_failed"] = False
                existing["failed"] = False
                existing["failure_reason"] = ""
                existing["failed_at"] = None
                existing["mail_healthy"] = None
                existing["mail_health_error"] = ""
                existing["mail_checked_at"] = None
                account_ids.append(str(existing.get("id") or ""))
                updated += 1
            else:
                item: dict[str, Any] = {
                    "id": f"hm_{uuid.uuid4().hex[:16]}",
                    **row,
                    "email": base_email,
                    "used": False,
                    "use_count": 0,
                    "used_aliases": [],
                    "failed_aliases": [],
                    "failed": False,
                    "account_failed": False,
                    "failure_reason": "",
                    "failed_at": None,
                    "created_at": time.time(),
                    "last_used_at": None,
                    "mail_healthy": None,
                    "mail_health_error": "",
                    "mail_checked_at": None,
                }
                accounts.append(item)
                by_email[base_email] = item
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
        rows = []
        available_slots = 0
        exhausted = 0
        account_failed = 0
        for item in accounts:
            account_id = str(item.get("id") or "")
            use_count = _normalize_use_count(item)
            remaining = _remaining_uses(item)
            reserved_set = _reserved_indices(account_id)
            reserved = bool(reserved_set)
            free_index = _free_alias_index(item, account_id)
            account_failed_flag = _account_level_failed(item)
            failed_aliases = _normalize_failed_aliases(item)
            fully_used = _is_fully_exhausted(item)
            if fully_used:
                exhausted += 1
            if account_failed_flag:
                account_failed += 1
            if free_index is not None:
                # Free slots excluding in-flight reservations and failed aliases.
                available_slots += max(
                    0,
                    ALIAS_USES
                    - len(_normalize_used_aliases(item) | failed_aliases | reserved_set),
                )
            next_email = (
                build_plus_alias(str(item.get("email") or ""), free_index)
                if free_index is not None
                else ""
            )
            rows.append({
                "id": item.get("id"),
                "email": item.get("email"),
                "base_email": base_mailbox_email(str(item.get("email") or "")),
                "used": fully_used,
                "use_count": use_count,
                "use_limit": ALIAS_USES,
                "remaining_uses": remaining if free_index is not None else 0,
                "next_alias_email": next_email,
                "failed": account_failed_flag,
                "failed_aliases": sorted(failed_aliases),
                "failure_reason": str(item.get("failure_reason") or ""),
                "failed_at": item.get("failed_at"),
                "reserved": reserved,
                "has_password": bool(item.get("password")),
                "client_id_masked": _masked(str(item.get("client_id") or "")),
                "refresh_token_masked": _masked(str(item.get("refresh_token") or "")),
                "created_at": item.get("created_at"),
                "last_used_at": item.get("last_used_at"),
                "mail_healthy": item.get("mail_healthy") if isinstance(item.get("mail_healthy"), bool) else None,
                "mail_health_error": str(item.get("mail_health_error") or ""),
                "mail_checked_at": item.get("mail_checked_at"),
                "verification_entries": _verification_entries(account_id),
            })
    return {
        "ok": True,
        "accounts": rows,
        "total": len(rows),
        # Capacity for new registrations = remaining plus-address slots.
        "available": available_slots,
        "available_accounts": sum(
            1
            for item in rows
            if not item["used"]
            and not item["failed"]
            and item["mail_healthy"] is not False
            and int(item.get("remaining_uses") or 0) > 0
        ),
        "used": exhausted,
        "failed": account_failed,
        "healthy": sum(1 for item in rows if item["mail_healthy"] is True),
        "unhealthy": sum(1 for item in rows if item["mail_healthy"] is False),
        "unchecked": sum(1 for item in rows if item["mail_healthy"] is None),
        "alias_uses": ALIAS_USES,
    }


def reserve_account() -> dict[str, Any]:
    """Reserve one plus-alias slot, spreading across physical mailboxes first.

    Preference order:
    1. Mailboxes with zero in-flight reservations (round-robin)
    2. Then other free alias slots on already-busy mailboxes
    """
    global _reserve_cursor
    with _lock:
        accounts = _load()
        if not accounts:
            raise RuntimeError("微软邮箱账户池没有可用账号，请先导入或恢复未用账号")

        def candidates(prefer_idle: bool) -> list[tuple[dict[str, Any], str, int]]:
            found: list[tuple[dict[str, Any], str, int]] = []
            total = len(accounts)
            for offset in range(total):
                item = accounts[(_reserve_cursor + offset) % total]
                account_id = str(item.get("id") or "")
                if not account_id:
                    continue
                if not item.get("email") or not item.get("client_id") or not item.get("refresh_token"):
                    continue
                if prefer_idle and _reserved_indices(account_id):
                    continue
                alias_index = _free_alias_index(item, account_id)
                if alias_index is None:
                    continue
                found.append((item, account_id, alias_index))
            return found

        ordered = candidates(prefer_idle=True) or candidates(prefer_idle=False)
        if not ordered:
            raise RuntimeError("微软邮箱账户池没有可用账号，请先导入或恢复未用账号")

        item, account_id, alias_index = ordered[0]
        # Advance cursor past the chosen mailbox so the next reserve starts elsewhere.
        try:
            chosen_pos = next(
                index
                for index, row in enumerate(accounts)
                if str(row.get("id") or "") == account_id
            )
            _reserve_cursor = (chosen_pos + 1) % max(1, len(accounts))
        except StopIteration:
            _reserve_cursor = (_reserve_cursor + 1) % max(1, len(accounts))

        _reservations.setdefault(account_id, set()).add(alias_index)
        reserved = dict(item)
        reserved["email"] = base_mailbox_email(str(item.get("email") or ""))
        reserved["use_count"] = _normalize_use_count(item)
        reserved["alias_index"] = alias_index
        reserved["registration_email"] = build_plus_alias(
            str(item.get("email") or ""), alias_index
        )
        reserved["base_email"] = base_mailbox_email(str(item.get("email") or ""))
        return reserved


def release_account(account_id: str, alias_index: int | None = None) -> None:
    account_id = str(account_id or "")
    with _lock:
        held = _reservations.get(account_id)
        if not held:
            return
        if alias_index is None:
            # Release one arbitrary reservation (legacy callers).
            held.pop()
        else:
            held.discard(int(alias_index))
        if not held:
            _reservations.pop(account_id, None)


def _is_account_level_failure_reason(reason: str) -> bool:
    """Only definite mailbox credential / token failures freeze the whole account.

    Page-flow errors (e.g. login button timeout, OAuth page stall) must stay
    alias-scoped so remaining plus-addresses remain schedulable.
    """
    text = str(reason or "").strip().lower()
    if not text:
        return False
    markers = (
        "invalid_grant",
        "aadsts",
        "refresh token",
        "refresh_token",
        "unauthorized",
        "凭证失效",
        "令牌失效",
        "令牌已撤销",
        "刷新令牌",
        "邮箱凭证",
        "邮箱不可用",
        "mailbox unavailable",
        "token revoked",
        "token expired",
        "token invalid",
    )
    return any(marker in text for marker in markers)


def mark_used(account_id: str, alias_index: int | None = None) -> bool:
    """Consume one plus-address slot on successful registration.

    The base mailbox stays reusable until ALIAS_USES successful registrations.
    """
    account_id = str(account_id or "")
    with _lock:
        accounts = _load()
        found = False
        for item in accounts:
            if str(item.get("id") or "") == account_id:
                used_aliases = _normalize_used_aliases(item)
                failed_aliases = _normalize_failed_aliases(item)
                chosen = _coerce_alias_index(alias_index)
                if chosen is None and alias_index is None:
                    held = _reservations.get(account_id) or set()
                    chosen = min(held) if held else None
                    chosen = _coerce_alias_index(chosen)
                if chosen is None and alias_index is None:
                    # Fallback: append next sequential free slot.
                    blocked = used_aliases | failed_aliases
                    chosen = min(
                        (index for index in range(ALIAS_USES) if index not in blocked),
                        default=None,
                    )
                if chosen is not None:
                    used_aliases.add(chosen)
                    failed_aliases.discard(chosen)
                item["used_aliases"] = sorted(used_aliases)
                item["failed_aliases"] = sorted(failed_aliases)
                item["use_count"] = len(used_aliases)
                _sync_used_flag(item)
                # Successful alias must not clear true account-level auth failures.
                if not _account_level_failed(item):
                    item["failed"] = False
                    item["failure_reason"] = ""
                    item["failed_at"] = None
                item["last_used_at"] = time.time()
                found = True
                break
        held = _reservations.get(account_id)
        if held is not None:
            if alias_index is None:
                if held:
                    held.pop()
            else:
                coerced = _coerce_alias_index(alias_index)
                if coerced is not None:
                    held.discard(coerced)
            if not held:
                _reservations.pop(account_id, None)
        if found:
            _save(accounts)
        return found


def mark_failed(account_id: str, reason: str = "", alias_index: int | None = None) -> bool:
    """Record failure.

    - With alias_index: only that plus-alias is blocked; other aliases stay free.
    - Without alias_index / account-level reason: quarantine whole mailbox.
    """
    account_id = str(account_id or "")
    reason_text = str(reason or "注册失败")[:500]
    with _lock:
        accounts = _load()
        found = False
        for item in accounts:
            if str(item.get("id") or "") == account_id:
                used_aliases = _normalize_used_aliases(item)
                failed_aliases = _normalize_failed_aliases(item)
                item["used_aliases"] = sorted(used_aliases)
                item["use_count"] = len(used_aliases)
                coerced_alias = _coerce_alias_index(alias_index)
                account_level = coerced_alias is None or _is_account_level_failure_reason(
                    reason_text
                )
                if account_level:
                    item["account_failed"] = True
                    item["failed"] = True
                    item["failed_aliases"] = sorted(failed_aliases)
                    item["failure_reason"] = reason_text
                    item["failed_at"] = time.time()
                else:
                    failed_aliases.add(coerced_alias)
                    item["failed_aliases"] = sorted(failed_aliases)
                    # Keep mailbox schedulable for remaining aliases.
                    if not _account_level_failed(item):
                        item["failed"] = False
                        if not item.get("failure_reason"):
                            item["failure_reason"] = ""
                    item["last_alias_failure_reason"] = reason_text
                    item["last_alias_failed_at"] = time.time()
                _sync_used_flag(item)
                found = True
                break
        held = _reservations.get(account_id)
        if held is not None:
            if alias_index is None:
                if held:
                    held.pop()
            else:
                coerced = _coerce_alias_index(alias_index)
                if coerced is not None:
                    held.discard(coerced)
            if not held:
                _reservations.pop(account_id, None)
        if found:
            _save(accounts)
        return found


def get_refresh_token(account_id: str) -> str:
    """Return the latest persisted refresh token for an account."""
    account_id = str(account_id or "")
    if not account_id:
        return ""
    with _lock:
        for item in _load():
            if str(item.get("id") or "") == account_id:
                return str(item.get("refresh_token") or "")
    return ""


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


def rotate_refresh_token(account_id: str, refresh_token: str) -> str:
    """Persist a rotated token under the per-account token lock and return it."""
    account_id = str(account_id or "")
    refresh_token = str(refresh_token or "").strip()
    if not account_id or not refresh_token:
        return get_refresh_token(account_id)
    with _account_token_lock(account_id):
        update_refresh_token(account_id, refresh_token)
        return refresh_token


def set_used(account_id: str, used: bool) -> bool:
    """Mark an account fully used, or restore it for another full alias cycle."""
    account_id = str(account_id or "")
    with _lock:
        accounts = _load()
        found = False
        for item in accounts:
            if str(item.get("id") or "") == account_id:
                if used:
                    item["used_aliases"] = list(range(ALIAS_USES))
                    item["failed_aliases"] = []
                    item["use_count"] = ALIAS_USES
                    item["used"] = True
                    item["failed"] = False
                    item["account_failed"] = False
                    item["failure_reason"] = ""
                    item["failed_at"] = None
                else:
                    # Clear quarantine. Only reset alias cycle when fully exhausted.
                    item["failed"] = False
                    item["account_failed"] = False
                    item["failure_reason"] = ""
                    item["failed_at"] = None
                    item["failed_aliases"] = []
                    used_aliases = _normalize_used_aliases(item)
                    if len(used_aliases) >= ALIAS_USES:
                        item["used_aliases"] = []
                        item["use_count"] = 0
                        item["last_used_at"] = None
                    else:
                        item["used_aliases"] = sorted(used_aliases)
                        item["use_count"] = len(used_aliases)
                    item["used"] = False
                found = True
                break
        if not used:
            _reservations.pop(account_id, None)
        if found:
            _save(accounts)
        return found


def delete_account(account_id: str) -> bool:
    """Delete one mailbox.

    Raises RuntimeError when the account is reserved by an in-flight registration.
    Returns False when the account does not exist.
    """
    account_id = str(account_id or "")
    with _lock:
        accounts = _load()
        if not any(str(item.get("id") or "") == account_id for item in accounts):
            return False
        if account_id in _reservations:
            raise RuntimeError("邮箱正在注册任务中使用，无法删除")
        kept = [item for item in accounts if str(item.get("id") or "") != account_id]
        _drop_token_lock(account_id)
        _verification_states.pop(account_id, None)
        _save(kept)
        return True


def delete_used_accounts() -> dict[str, int]:
    """Delete exhausted mailboxes (all aliases used or failed) not currently reserved."""
    with _lock:
        accounts = _load()
        kept: list[dict[str, Any]] = []
        deleted = 0
        skipped_reserved = 0
        removed_ids: list[str] = []
        for item in accounts:
            account_id = str(item.get("id") or "")
            exhausted = _is_fully_exhausted(item)
            if exhausted and account_id not in _reservations:
                deleted += 1
                removed_ids.append(account_id)
                continue
            if exhausted and account_id in _reservations:
                skipped_reserved += 1
            kept.append(item)
        if deleted:
            for account_id in removed_ids:
                _drop_token_lock(account_id)
                _verification_states.pop(account_id, None)
            _save(kept)
        return {"deleted": deleted, "skipped_reserved": skipped_reserved}


def delete_unhealthy_accounts() -> dict[str, int]:
    """Delete accounts whose latest mailbox probe failed."""
    with _lock:
        accounts = _load()
        kept: list[dict[str, Any]] = []
        deleted = 0
        skipped_reserved = 0
        removed_ids: list[str] = []
        for item in accounts:
            account_id = str(item.get("id") or "")
            unhealthy = item.get("mail_healthy") is False
            if unhealthy and account_id not in _reservations:
                deleted += 1
                removed_ids.append(account_id)
                continue
            if unhealthy and account_id in _reservations:
                skipped_reserved += 1
            kept.append(item)
        if deleted:
            for account_id in removed_ids:
                _drop_token_lock(account_id)
                _verification_states.pop(account_id, None)
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
    with _account_token_lock(account_id):
        refresh_token = get_refresh_token(account_id) or str(account.get("refresh_token") or "")
        try:
            result = _request_json(
                base_url or DEFAULT_HELPER_URL,
                "/messages",
                {
                    "email": account.get("email"),
                    "clientId": account.get("client_id"),
                    "refreshToken": refresh_token,
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
        next_token = str(result.get("nextRefreshToken") or "").strip()
        with _lock:
            accounts = _load()
            for item in accounts:
                if str(item.get("id") or "") != account_id:
                    continue
                if next_token:
                    item["refresh_token"] = next_token
                item["mail_healthy"] = healthy
                item["mail_health_error"] = error[:500]
                item["mail_checked_at"] = checked_at
                if healthy:
                    # Working credentials clear account-level token quarantine.
                    item["account_failed"] = False
                    item["failed"] = False
                    item["failure_reason"] = ""
                    item["failed_at"] = None
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
        self.base_email = base_mailbox_email(
            str(account.get("base_email") or account.get("email") or "")
        )
        # Registration address may be a plus-alias; Graph login still uses base mailbox.
        self.email = str(
            account.get("registration_email") or account.get("email") or ""
        ).strip().lower()
        if not self.email:
            self.email = self.base_email
        self.password = str(account.get("password") or "")
        self.client_id = str(account.get("client_id") or "")
        self.refresh_token = str(account.get("refresh_token") or "")
        try:
            self.alias_index = int(account.get("alias_index") or 0)
        except (TypeError, ValueError):
            self.alias_index = 0
        self.base_url = str(base_url or DEFAULT_HELPER_URL).rstrip("/")
        self.verification_target = (
            "xai" if str(verification_target or "").strip().lower() == "xai" else "chatgpt"
        )
        self.created_at_ms = int(time.time() * 1000)

    def mark_code_request_started(self) -> None:
        """Ignore messages from before the current verification-code request."""
        # Small clock-skew allowance for provider timestamps.
        self.created_at_ms = int(time.time() * 1000) - 5000
        _set_verification_state(
            self.account_id, self.alias_index, self.email, "waiting"
        )

    def wait_for_code(self, timeout: float = 120, *, should_cancel=None, poll_interval: float | None = None,
                      exclude_codes: set[str] | None = None) -> str:
        _set_verification_state(
            self.account_id, self.alias_index, self.email, "waiting"
        )
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
        token_lock = _account_token_lock(self.account_id)
        while time.time() < deadline:
            if callable(should_cancel) and should_cancel():
                _set_verification_state(self.account_id, self.alias_index, self.email, "cancelled")
                raise RuntimeError("cancelled while waiting for email code")
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            # Timed, slice-wise acquire so pause/cancel/deadline can interrupt waiters
            # blocked behind a long concurrent mailbox request on the same account.
            acquired = token_lock.acquire(timeout=min(0.25, remaining))
            if not acquired:
                continue
            try:
                if callable(should_cancel) and should_cancel():
                    _set_verification_state(self.account_id, self.alias_index, self.email, "cancelled")
                    raise RuntimeError("cancelled while waiting for email code")
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                # Bound by remaining deadline only; never force an 8s floor past expiry.
                request_timeout = min(35.0, remaining)
                latest = get_refresh_token(self.account_id)
                if latest:
                    self.refresh_token = latest
                payload = {
                    # Helper authenticates with the real mailbox account.
                    "email": self.base_email or self.email,
                    "clientId": self.client_id,
                    "refreshToken": self.refresh_token,
                    "mailboxes": ["INBOX"],
                    # Helper max is 30; cover full alias concurrency plus resend headroom.
                    "top": _code_fetch_top(self.account_id),
                    "senderFilters": sender_filters,
                    "subjectFilters": subject_filters,
                    # Match the registration alias so concurrent +N tasks don't share codes.
                    "recipientFilters": [self.email] if self.email else [],
                    "excludeCodes": sorted(helper_excluded),
                    "filterAfterTimestamp": self.created_at_ms,
                    "allowTimeFallback": False,
                    "requiredKeywords": required_keywords,
                    "codePatterns": code_patterns,
                }
                try:
                    result = _request_json(
                        self.base_url,
                        "/code",
                        payload,
                        timeout=request_timeout,
                    )
                    next_token = str(result.get("nextRefreshToken") or "").strip()
                    if next_token:
                        self.refresh_token = next_token
                        update_refresh_token(self.account_id, next_token)
                    code = str(result.get("code") or "").strip()
                    normalized = re.sub(r"[^A-Za-z0-9]", "", code).upper()
                    if is_xai:
                        valid = bool(re.fullmatch(r"[A-Z0-9]{6}", normalized))
                        if result.get("ok") and valid and normalized not in normalized_excluded:
                            _set_verification_state(
                                self.account_id, self.alias_index, self.email, "received", code=normalized
                            )
                            return normalized
                    elif result.get("ok") and re.fullmatch(r"\d{6}", code) and code not in excluded:
                        _set_verification_state(
                            self.account_id, self.alias_index, self.email, "received", code=code
                        )
                        return code
                    last_error = str(result.get("error") or result.get("message") or "")
                except Exception as exc:
                    last_error = str(exc)
            finally:
                token_lock.release()
            slept = 0.0
            while slept < poll and time.time() < deadline:
                if callable(should_cancel) and should_cancel():
                    _set_verification_state(self.account_id, self.alias_index, self.email, "cancelled")
                    raise RuntimeError("cancelled while waiting for email code")
                step = min(0.25, poll - slept)
                time.sleep(step)
                slept += step
        if callable(should_cancel) and should_cancel():
            _set_verification_state(self.account_id, self.alias_index, self.email, "cancelled")
            raise RuntimeError("cancelled while waiting for email code")
        suffix = f": {last_error}" if last_error else ""
        _set_verification_state(
            self.account_id,
            self.alias_index,
            self.email,
            "failed",
            error=last_error or "等待验证码超时",
        )
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
    # Return the plus-alias used for signup; inbox reads still hit the base mailbox.
    return receiver.email, receiver
