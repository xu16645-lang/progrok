"""Extracted application core services."""

from __future__ import annotations

from typing import Any



def load_config(ctx):
    with ctx._config_lock:
        data = dict(ctx.DEFAULT_CONFIG)
        loaded: dict[str, Any] = {}
        if ctx.CONFIG_FILE.exists():
            try:
                loaded = ctx.json.loads(ctx.CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(
                        {k: v for k, v in loaded.items() if k in ctx.DEFAULT_CONFIG}
                    )
            except Exception:
                pass
        provider = str(data.get("mail_provider") or "").lower()
        data["mail_provider"] = (
            provider
            if provider
            in {
                "yyds",
                "custom",
                "cloudflare_grokfree",
                "stalwart",
                "hotmail_local",
            }
            else "custom"
        )
        raw_profiles = data.get("mail_provider_configs")
        profiles = dict(raw_profiles) if isinstance(raw_profiles, dict) else {}
        if "mail_provider_configs" not in loaded:
            migrated_provider = (
                "yyds"
                if "maliapi.215.im" in str(data.get("mail_base_url") or "")
                else data["mail_provider"]
            )
            if migrated_provider in {
                "yyds",
                "custom",
                "cloudflare_grokfree",
                "stalwart",
            }:
                profiles[migrated_provider] = {
                    "mail_base_url": str(data.get("mail_base_url") or ""),
                    "mail_api_key": str(data.get("mail_api_key") or ""),
                    "mail_domain": str(data.get("mail_domain") or ""),
                }
        data["mail_provider_configs"] = ctx._normalize_mail_provider_configs(profiles)
        if "registration_json_format" not in loaded:
            data["registration_json_format"] = data.get("auto_import_target", "cpa")
        if "probe_concurrency" not in loaded:
            data["probe_concurrency"] = data.get("concurrency", 1)
        if "import_concurrency" not in loaded:
            data["import_concurrency"] = data.get("concurrency", 1)
        if "chatgpt_headless" not in loaded:
            data["chatgpt_headless"] = True
        if "grok_headless" not in loaded:
            data["grok_headless"] = True
        # ChatGPT registration is temporarily disabled. Existing account,
        # import and probe data remain available; only the registration target
        # is migrated back to Grok.
        data["registration_target"] = "grok"
        data["registration_mode"] = "browser"
        selected_format = str(data.get("registration_json_format") or "cpa").lower()
        data["registration_json_format"] = (
            selected_format if selected_format in {"cpa", "sub2api"} else "cpa"
        )
        data["auto_import_target"] = data["registration_json_format"]
        data["sub2api_chatgpt_models"] = list(ctx.CHATGPT_SUB2API_MODELS)
        return data


def _sync_solver_proxy_file(ctx, cfg):
    from proxy_pool import parse_proxy_pool

    proxies = parse_proxy_pool(
        str(cfg.get("proxy") or ""),
        username=str(cfg.get("proxy_username") or ""),
        password=str(cfg.get("proxy_password") or ""),
        fallback_env=False,
    )
    if not proxies:
        ctx.SOLVER_PROXY_FILE.unlink(missing_ok=True)
        return 0
    ctx.SOLVER_PROXY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ctx.SOLVER_PROXY_FILE.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(proxies) + "\n", encoding="utf-8")
    ctx.os.replace(tmp, ctx.SOLVER_PROXY_FILE)
    return len(proxies)


def save_config(ctx, data):
    clean = {
        **ctx.DEFAULT_CONFIG,
        **{k: v for k, v in data.items() if k in ctx.DEFAULT_CONFIG},
    }
    profiles = ctx._normalize_mail_provider_configs(clean.get("mail_provider_configs"))
    active_provider = str(clean.get("mail_provider") or "").lower()
    if active_provider in {"yyds", "custom", "cloudflare_grokfree", "stalwart"}:
        profiles[active_provider] = {
            "mail_base_url": str(clean.get("mail_base_url") or ""),
            "mail_api_key": str(clean.get("mail_api_key") or ""),
            "mail_domain": str(clean.get("mail_domain") or ""),
        }
    clean["mail_provider_configs"] = profiles
    selected_format = str(clean.get("registration_json_format") or "cpa").lower()
    clean["registration_json_format"] = (
        selected_format if selected_format in {"cpa", "sub2api"} else "cpa"
    )
    clean["auto_import_target"] = clean["registration_json_format"]
    clean["registration_target"] = "grok"
    clean["registration_mode"] = "browser"
    clean["sub2api_chatgpt_models"] = list(ctx.CHATGPT_SUB2API_MODELS)
    with ctx._config_lock:
        ctx.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ctx.CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            ctx.json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        ctx.os.replace(tmp, ctx.CONFIG_FILE)
    ctx.apply_environment(clean)
    ctx._sync_solver_proxy_file(clean)
    return clean


def _normalize_mail_provider_configs(ctx, value):
    raw = value if isinstance(value, dict) else {}
    result: dict[str, dict[str, str]] = {}
    defaults = ctx.DEFAULT_CONFIG.get("mail_provider_configs") or {}
    for provider in ("yyds", "custom", "cloudflare_grokfree", "stalwart"):
        item = raw.get(provider) if isinstance(raw.get(provider), dict) else {}
        fallback = (
            defaults.get(provider) if isinstance(defaults.get(provider), dict) else {}
        )
        if provider == "cloudflare_grokfree" and not item:
            custom = raw.get("custom") if isinstance(raw.get("custom"), dict) else {}
            item = {
                **fallback,
                "mail_api_key": str(custom.get("mail_api_key") or ""),
            }
        result[provider] = {
            key: str(item.get(key, fallback.get(key, "")) or "")
            for key in ("mail_base_url", "mail_api_key", "mail_domain")
        }
    return result


def apply_environment(ctx, cfg):
    mapping = {
        "GROK2API_MOEMAIL_API_KEY": cfg.get("mail_api_key", ""),
        "GROK2API_MOEMAIL_BASE_URL": cfg.get("mail_base_url", ""),
        "GROK2API_MOEMAIL_DOMAIN": cfg.get("mail_domain", ""),
        "GROK2API_CAPTCHA_PROVIDER": cfg.get("captcha_provider", "local"),
        "GROK2API_LOCAL_SOLVER_URL": cfg.get(
            "local_solver_url", "http://127.0.0.1:5072"
        ),
        "GROK2API_YESCAPTCHA_KEY": cfg.get("yescaptcha_key", ""),
        "GROK2API_XAI_PROXY": cfg.get("proxy", ""),
        "GROK2API_XAI_PROXY_USERNAME": cfg.get("proxy_username", ""),
        "GROK2API_XAI_PROXY_PASSWORD": cfg.get("proxy_password", ""),
        "GROK2API_XAI_PROXY_STRATEGY": cfg.get("proxy_strategy", "round_robin"),
    }
    for key, value in mapping.items():
        ctx.os.environ[key] = str(value or "")


def _get_registration_adapter(ctx, target=None):
    """Return the registration adapter module for the given target.

    If target is None, reads from the current config.
    Modules are imported lazily so the API starts even if one adapter is unavailable.
    """
    if target is None:
        cfg = ctx.load_config()
        target = str(cfg.get("registration_target") or "grok").strip().lower()
    if target not in ("grok", "chatgpt"):
        target = "grok"
    if target in ctx._registration_adapters:
        return ctx._registration_adapters[target]
    if target == "chatgpt":
        import chatgpt_build_adapter as adapter
    else:
        import grok_build_adapter as adapter
    ctx._registration_adapters[target] = adapter
    return adapter


def _post_registration_config(ctx, cfg):
    registration_target = str(cfg.get("registration_target") or "grok").strip().lower()
    registration_mode = "browser"
    probe_model = (
        cfg.get("chatgpt_probe_model") or "gpt-5.5"
        if registration_target == "chatgpt"
        else cfg.get("probe_model") or "grok-4.5"
    )
    return {
        "model": probe_model,
        "registration_target": registration_target,
        "registration_mode": registration_mode,
        "step_delay_ms": int(cfg.get("chatgpt_step_delay_ms") or 0),
        "pipeline_concurrency": int(cfg.get("concurrency") or 1),
        "probe_concurrency": int(
            cfg.get("probe_concurrency") or cfg.get("concurrency") or 1
        ),
        "probe_stagger_ms": int(cfg.get("probe_stagger_ms") or 0),
        "import_concurrency": int(
            cfg.get("import_concurrency") or cfg.get("concurrency") or 1
        ),
        "import_stagger_ms": int(cfg.get("import_stagger_ms") or 0),
        "pre_import_probe_enabled": bool(
            cfg.get("pre_import_probe_enabled", True)
        ),
        "auto_import_enabled": bool(cfg.get("auto_import_enabled")),
        "target": cfg.get("auto_import_target") or "sub2api",
        "output_format": cfg.get("registration_json_format") or "cpa",
        "cpa_base_url": cfg.get("cpa_base_url") or "",
        "cpa_management_key": cfg.get("cpa_management_key") or "",
        "sub2api_base_url": cfg.get("sub2api_base_url") or "",
        "sub2api_auth_mode": cfg.get("sub2api_auth_mode") or "password",
        "sub2api_admin_email": cfg.get("sub2api_admin_email") or "",
        "sub2api_admin_password": cfg.get("sub2api_admin_password") or "",
        "sub2api_api_key": cfg.get("sub2api_api_key") or "",
        "sub2api_group_id": int(cfg.get("sub2api_group_id") or 0),
        "sub2api_group_name": cfg.get("sub2api_group_name") or "",
        "sub2api_xai_group_id": int(cfg.get("sub2api_xai_group_id") or 0),
        "sub2api_xai_group_name": cfg.get("sub2api_xai_group_name") or "",
        "sub2api_chatgpt_models": list(ctx.CHATGPT_SUB2API_MODELS),
    }


def _manual_import_records(ctx, payload):
    if isinstance(payload, dict) and payload.get("type") == "xai":
        return [payload]
    if isinstance(payload, dict) and payload.get("type") == "chatgpt":
        return [payload]
    if isinstance(payload, dict) and payload.get("type") == "sub2api-data":
        records: list[dict[str, Any]] = []
        for account in payload.get("accounts") or []:
            if not isinstance(account, dict):
                continue
            credentials = account.get("credentials") or {}
            extra = account.get("extra") or {}
            if isinstance(credentials, dict) and isinstance(extra, dict):
                record = {**credentials, **extra}
                platform = str(account.get("platform") or "").strip().lower()
                account_type = str(account.get("type") or "").strip().lower()
                record["source_platform"] = platform
                record["source_account_type"] = account_type
                records.append(record)
        return records
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise ValueError("仅支持 CPA JSON、Sub2API JSON 或账号 JSON 数组")


def _normalize_import_record_for_registration_target(ctx, record, registration_target):
    target = "chatgpt" if str(registration_target).lower() == "chatgpt" else "grok"
    identity = (
        record.get("agent_identity")
        if isinstance(record.get("agent_identity"), dict)
        else record
    )
    has_agent_identity = bool(
        isinstance(identity, dict)
        and identity.get("agent_runtime_id")
        and identity.get("agent_private_key")
    )
    if target == "chatgpt":
        if not has_agent_identity:
            raise ValueError(
                "当前注册目标为 ChatGPT，但导入文件不是 Agent Identity 格式"
            )
        return {
            **record,
            "type": "chatgpt",
            "auth_kind": "agent_identity",
            "agent_identity": dict(identity),
        }
    if has_agent_identity:
        raise ValueError(
            "当前注册目标为 Grok，但导入文件是 ChatGPT Agent Identity，请先切换注册目标"
        )
    return {**record, "type": "xai"}


def _rotation_session_items(ctx):
    items: list[tuple[str, dict[str, Any]]] = []
    for target in ("grok", "chatgpt"):
        try:
            data = ctx._get_registration_adapter(target).list_registration_sessions()
        except Exception:
            continue
        sessions = data.get("sessions") if isinstance(data, dict) else None
        if isinstance(sessions, dict):
            sessions = list(sessions.values())
        if not isinstance(sessions, list):
            continue
        account_type = "chatgpt" if target == "chatgpt" else "xai"
        items.extend(
            (
                (account_type, session)
                for session in sessions
                if isinstance(session, dict)
            )
        )
    return items


def _sync_account_rotation(ctx):
    return ctx.sync_imported_sessions(ctx._rotation_session_items())


def start_account_rotation(ctx):
    ctx._sync_account_rotation()
    ctx.start_rotation_scheduler(ctx._sync_account_rotation, ctx.load_config)


def stop_account_rotation(ctx):
    ctx.stop_rotation_scheduler()
