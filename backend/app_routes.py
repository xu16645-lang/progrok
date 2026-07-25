"""Extracted application routes services."""

from __future__ import annotations

from typing import Any



def get_config(ctx):
    return ctx.load_config()


def sub2api_groups(ctx, request):
    from account_pipeline import list_sub2api_groups

    result = list_sub2api_groups(
        base_url=request.sub2api_base_url,
        api_key=request.sub2api_api_key,
        auth_mode=request.sub2api_auth_mode,
        admin_email=request.sub2api_admin_email,
        admin_password=request.sub2api_admin_password,
    )
    if not result.get("ok"):
        raise ctx.HTTPException(status_code=400, detail=result)
    return result


def performance_profile(ctx, provider=None):
    cfg = ctx.load_config()
    adapter = ctx._get_registration_adapter()
    return adapter.get_registration_performance_profile(
        provider or cfg.get("captcha_provider"), cfg.get("local_solver_url")
    )


def mail_provider_presets(ctx, response):
    response.headers["Cache-Control"] = "no-store"
    return {
        "providers": {
            "yyds": {
                "available": True,
                "mail_base_url": "",
                "mail_api_key": "",
                "mail_domain": "",
            },
            "custom": {
                "available": True,
                "mail_base_url": "",
                "mail_api_key": "",
                "mail_domain": "",
            },
            "cloudflare_grokfree": {
                "available": True,
                "mail_base_url": "",
                "mail_api_key": "",
                "mail_domain": "",
            },
            "stalwart": {
                "available": True,
                "mail_base_url": "",
                "mail_api_key": "",
                "mail_domain": "",
            },
            "hotmail_local": {
                "available": True,
                "hotmail_local_base_url": "http://127.0.0.1:17373",
            },
        }
    }


def hotmail_accounts(ctx):
    from hotmail_local import list_accounts

    return list_accounts()


def hotmail_import(ctx, request):
    from hotmail_local import import_accounts, list_accounts, probe_accounts

    result = import_accounts(request.text)
    account_ids = result.pop("account_ids", [])
    base_url = request.base_url.strip() or str(
        ctx.load_config().get("hotmail_local_base_url") or ""
    )
    result["probe"] = probe_accounts(base_url, account_ids)
    result["pool"] = list_accounts()
    return result


def hotmail_probe_all(ctx, request):
    from hotmail_local import list_accounts, probe_accounts

    base_url = request.base_url.strip() or str(
        ctx.load_config().get("hotmail_local_base_url") or ""
    )
    result = probe_accounts(base_url)
    result["pool"] = list_accounts()
    return result


def hotmail_reset_health(ctx):
    from hotmail_local import list_accounts, reset_healthy_accounts

    reset = reset_healthy_accounts()
    return {"ok": True, "reset": reset, "pool": list_accounts()}


def hotmail_probe_one(ctx, account_id, request):
    from hotmail_local import list_accounts, probe_account

    base_url = request.base_url.strip() or str(
        ctx.load_config().get("hotmail_local_base_url") or ""
    )
    result = probe_account(account_id, base_url)
    if result.get("error") == "邮箱账号不存在":
        raise ctx.HTTPException(status_code=404, detail=result["error"])
    result["pool"] = list_accounts()
    return result


def hotmail_set_status(ctx, account_id, request):
    from hotmail_local import list_accounts, set_preferred_account, set_used

    try:
        if request.preferred_for_next_use is True:
            if not set_preferred_account(account_id):
                raise ctx.HTTPException(status_code=404, detail="邮箱账号不存在")
        elif request.used is not None:
            if not set_used(account_id, request.used):
                raise ctx.HTTPException(status_code=404, detail="邮箱账号不存在")
        else:
            raise ctx.HTTPException(status_code=400, detail="没有可更新的邮箱状态")
    except RuntimeError as exc:
        raise ctx.HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "pool": list_accounts()}


def hotmail_delete_used(ctx):
    from hotmail_local import delete_used_accounts, list_accounts

    result = delete_used_accounts()
    return {"ok": True, **result, "pool": list_accounts()}


def hotmail_delete_unhealthy(ctx):
    from hotmail_local import delete_unhealthy_accounts, list_accounts

    result = delete_unhealthy_accounts()
    return {"ok": True, **result, "pool": list_accounts()}


def hotmail_delete(ctx, account_id):
    from hotmail_local import delete_account, list_accounts

    try:
        if not delete_account(account_id):
            raise ctx.HTTPException(status_code=404, detail="邮箱账号不存在")
    except RuntimeError as exc:
        raise ctx.HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "pool": list_accounts()}


def hotmail_test(ctx, settings=None):
    from hotmail_local import helper_health

    cfg = settings.model_dump() if settings else ctx.load_config()
    try:
        return helper_health(str(cfg.get("hotmail_local_base_url") or ""))
    except Exception as exc:
        raise ctx.HTTPException(status_code=400, detail=str(exc)) from exc


def put_config(ctx, settings):
    return {"ok": True, "config": ctx.save_config(settings.model_dump())}


def start_register(ctx, settings=None, paused=False):
    cfg = settings.model_dump() if settings else ctx.load_config()
    requested_target = str(cfg.get("registration_target") or "grok").strip().lower()
    if requested_target == "chatgpt":
        raise ctx.HTTPException(status_code=400, detail="ChatGPT 注册暂时停用")
    cfg["registration_mode"] = "browser"
    selected_format = str(
        cfg.get("registration_json_format") or cfg.get("auto_import_target") or "cpa"
    ).lower()
    cfg["registration_json_format"] = (
        selected_format if selected_format in {"cpa", "sub2api"} else "cpa"
    )
    cfg["auto_import_target"] = cfg["registration_json_format"]
    target = str(cfg.get("registration_target") or "grok").strip().lower()
    if target not in ("grok", "chatgpt"):
        target = "grok"
    if cfg.get("mail_provider") == "hotmail_local":
        from hotmail_local import list_accounts

        pool = list_accounts()
        available = int(pool.get("available") or 0)
        available_accounts = int(pool.get("available_accounts") or 0)
        if available < 1:
            raise ctx.HTTPException(
                status_code=400, detail="微软邮箱账户池没有可用邮箱，请先导入或恢复邮箱"
            )
        requested_count = int(cfg.get("count") or 0)
        if requested_count < 1:
            raise ctx.HTTPException(status_code=400, detail="注册数量必须大于 0")
        cfg["count"] = min(requested_count, available)
        cfg["concurrency"] = min(
            max(1, available_accounts), max(1, int(cfg.get("concurrency") or 1))
        )
    elif int(cfg.get("count") or 0) < 1:
        raise ctx.HTTPException(status_code=400, detail="注册数量必须大于 0")
    cfg["concurrency"] = min(
        8, int(cfg["count"]), max(1, int(cfg.get("concurrency") or 1))
    )
    if settings is not None:
        persisted = ctx.load_config()
        persisted["count"] = cfg["count"]
        persisted["concurrency"] = cfg["concurrency"]
        persisted["auto_tune_enabled"] = cfg["auto_tune_enabled"]
        persisted["probe_concurrency"] = cfg["probe_concurrency"]
        persisted["probe_model"] = cfg["probe_model"]
        persisted["chatgpt_probe_model"] = cfg["chatgpt_probe_model"]
        persisted["import_concurrency"] = cfg["import_concurrency"]
        persisted["registration_target"] = cfg["registration_target"]
        persisted["registration_mode"] = cfg["registration_mode"]
        persisted["sub2api_group_id"] = cfg["sub2api_group_id"]
        persisted["sub2api_group_name"] = cfg["sub2api_group_name"]
        persisted["sub2api_xai_group_id"] = cfg["sub2api_xai_group_id"]
        persisted["sub2api_xai_group_name"] = cfg["sub2api_xai_group_name"]
        persisted["grok_headless"] = cfg["grok_headless"]
        persisted["chatgpt_headless"] = cfg["chatgpt_headless"]
        ctx.save_config(persisted)
    ctx.apply_environment(cfg)
    ctx._sync_solver_proxy_file(cfg)
    adapter = ctx._get_registration_adapter(target)
    kwargs: dict[str, Any] = {
        "proxy": cfg["proxy"],
        "proxy_username": cfg["proxy_username"],
        "proxy_password": cfg["proxy_password"],
        "proxy_strategy": cfg["proxy_strategy"],
        "moemail_api_key": cfg["mail_api_key"],
        "moemail_base_url": cfg["mail_base_url"],
        "domain": cfg["mail_domain"],
        "expiry_ms": cfg["mail_expiry_ms"],
        "mail_provider": cfg["mail_provider"],
        "hotmail_local_base_url": cfg["hotmail_local_base_url"],
        "count": cfg["count"],
        "concurrency": cfg["concurrency"],
        "stagger_ms": 0 if target == "chatgpt" else cfg["stagger_ms"],
        "post_registration": ctx._post_registration_config(cfg),
        "start_paused": paused,
    }
    if target == "grok":
        kwargs.update(
            {
                "captcha_provider": cfg["captcha_provider"],
                "registration_mode": cfg["registration_mode"],
                "local_solver_url": cfg["local_solver_url"],
                "yescaptcha_key": cfg["yescaptcha_key"],
                "prefix": cfg["mail_prefix"],
                "probe_delay_sec": cfg["probe_delay_sec"],
                "auto_tune_enabled": cfg["auto_tune_enabled"],
                "headless": bool(cfg.get("grok_headless", True)),
            }
        )
    else:
        kwargs["headless"] = bool(cfg.get("chatgpt_headless", True))
    result = adapter.start_registration(**kwargs)
    if not result.get("ok"):
        raise ctx.HTTPException(status_code=400, detail=result)
    return result


def sessions(ctx):
    merged: dict[str, dict[str, Any]] = {"sessions": {}, "batches": {}}
    for target in ("grok", "chatgpt"):
        try:
            adapter = ctx._get_registration_adapter(target)
            data = adapter.list_registration_sessions()
            if isinstance(data, dict):
                for key in ("sessions", "batches"):
                    items = data.get(key)
                    if isinstance(items, dict):
                        items = list(items.values())
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        item_id = str(item.get("id") or item.get("batch_id") or "")
                        if item_id:
                            merged[key][item_id] = item
        except Exception:
            pass
    ctx._sync_account_rotation()
    return {
        key: sorted(
            items.values(),
            key=lambda item: float(
                item.get("updated_at") or item.get("created_at") or 0
            ),
            reverse=True,
        )
        for key, items in merged.items()
    }


def account_rotation_list(ctx, status=None, keyword=None, page=None, page_size=None):
    ctx._sync_account_rotation()
    return ctx.list_rotation_records(
        status=status, keyword=keyword, page=page, page_size=page_size
    )


def account_rotation_probe(ctx, request):
    ctx._sync_account_rotation()
    ids = None if request.all_accounts else request.ids
    if ids is not None and (not ids):
        raise ctx.HTTPException(status_code=400, detail="请至少选择一个账号")
    return ctx.schedule_rotation_probe(ids, ctx.load_config(), source="manual")


def account_rotation_probe_one(ctx, record_id):
    ctx._sync_account_rotation()
    return ctx.schedule_rotation_probe([record_id], ctx.load_config(), source="manual")


def account_rotation_delete(ctx, request):
    if not request.ids:
        raise ctx.HTTPException(status_code=400, detail="请至少选择一个账号")
    return ctx.delete_rotation_records(request.ids)


def reset_sessions(ctx):
    results = {}
    for target in ("grok", "chatgpt"):
        try:
            adapter = ctx._get_registration_adapter(target)
            results[target] = adapter.reset_registration_monitor()
        except Exception as e:
            results[target] = {"ok": False, "error": str(e)}
    return {"ok": True, "results": results}


def session(ctx, session_id):
    for target in ("grok", "chatgpt"):
        try:
            adapter = ctx._get_registration_adapter(target)
            result = adapter.get_registration_session(session_id)
            if result is not None:
                return result
        except Exception:
            pass
    raise ctx.HTTPException(status_code=404, detail="session not found")


def batch(ctx, batch_id):
    for target in ("grok", "chatgpt"):
        try:
            adapter = ctx._get_registration_adapter(target)
            result = adapter.get_registration_batch(batch_id)
            if result is not None:
                return result
        except Exception:
            pass
    raise ctx.HTTPException(status_code=404, detail="batch not found")


def stop_session(ctx, session_id):
    for target in ("grok", "chatgpt"):
        try:
            adapter = ctx._get_registration_adapter(target)
            result = adapter.stop_registration_session(session_id)
            if result.get("ok"):
                return result
        except Exception:
            pass
    raise ctx.HTTPException(status_code=404, detail="session not found")


def stop_batch(ctx, batch_id):
    for target in ("grok", "chatgpt"):
        try:
            adapter = ctx._get_registration_adapter(target)
            result = adapter.stop_registration_batch(batch_id)
            if result.get("ok"):
                return result
        except Exception:
            pass
    raise ctx.HTTPException(status_code=404, detail="batch not found")


def pause_batch(ctx, batch_id):
    for target in ("grok", "chatgpt"):
        try:
            adapter = ctx._get_registration_adapter(target)
            result = adapter.pause_registration_batch(batch_id)
            if result.get("ok"):
                return result
        except Exception:
            pass
    raise ctx.HTTPException(status_code=404, detail="batch not found")


def resume_batch(ctx, batch_id):
    for target in ("grok", "chatgpt"):
        try:
            adapter = ctx._get_registration_adapter(target)
            result = adapter.resume_registration_batch(batch_id, force=True)
            if result.get("ok"):
                return result
        except Exception:
            pass
    raise ctx.HTTPException(status_code=404, detail="batch not found")


def retry_session_probe(ctx, session_id):
    target = "chatgpt" if str(session_id).startswith("cgpt_") else "grok"
    adapter = ctx._get_registration_adapter(target)
    cfg = ctx.load_config()
    cfg["registration_target"] = target
    session = adapter.get_registration_session(session_id)
    if not session:
        raise ctx.HTTPException(status_code=404, detail="registration session not found")
    post_registration = ctx._post_registration_config(cfg)
    stored_import = (
        session.get("auto_import")
        if isinstance(session.get("auto_import"), dict)
        else {}
    )
    post_registration["auto_import_enabled"] = bool(stored_import.get("enabled"))
    post_registration["pre_import_probe_enabled"] = bool(
        session.get("pre_import_probe_enabled", True)
    )
    stored_target = str(
        stored_import.get("target") or session.get("registration_json_format") or ""
    ).lower()
    if stored_target in {"cpa", "sub2api"}:
        post_registration["target"] = stored_target
        post_registration["output_format"] = stored_target
    result = adapter.retry_registration_probe(
        session_id, post_registration
    )
    if not result.get("ok"):
        raise ctx.HTTPException(status_code=400, detail=result)
    return result


def retry_batch_probe(ctx, batch_id):
    target = "chatgpt" if str(batch_id).startswith("batch_cgpt_") else "grok"
    adapter = ctx._get_registration_adapter(target)
    cfg = ctx.load_config()
    cfg["registration_target"] = target
    result = adapter.retry_registration_batch_probe(
        batch_id, ctx._post_registration_config(cfg)
    )
    if not result.get("ok"):
        raise ctx.HTTPException(status_code=400, detail=result)
    return result


def pause_batch_probe(ctx, batch_id):
    target = "chatgpt" if str(batch_id).startswith("batch_cgpt_") else "grok"
    result = ctx._get_registration_adapter(target).pause_registration_batch_probe(
        batch_id
    )
    if not result.get("ok"):
        raise ctx.HTTPException(status_code=400, detail=result)
    return result


def resume_batch_probe(ctx, batch_id):
    target = "chatgpt" if str(batch_id).startswith("batch_cgpt_") else "grok"
    result = ctx._get_registration_adapter(target).resume_registration_batch_probe(
        batch_id
    )
    if not result.get("ok"):
        raise ctx.HTTPException(status_code=400, detail=result)
    return result


def retry_session_import(ctx, session_id):
    target = "chatgpt" if str(session_id).startswith("cgpt_") else "grok"
    adapter = ctx._get_registration_adapter(target)
    cfg = ctx.load_config()
    cfg["registration_target"] = target
    session = adapter.get_registration_session(session_id)
    if not session:
        raise ctx.HTTPException(status_code=404, detail="registration session not found")
    stored_target = str(
        (session.get("auto_import") or {}).get("target")
        if isinstance(session.get("auto_import"), dict)
        else ""
    ).lower()
    stored_format = str(session.get("registration_json_format") or "").lower()
    import_target = (
        stored_target
        if stored_target in {"cpa", "sub2api"}
        else stored_format if stored_format in {"cpa", "sub2api"} else "sub2api"
    )
    post_registration = ctx._post_registration_config(cfg)
    post_registration["target"] = import_target
    post_registration["output_format"] = import_target
    post_registration["pre_import_probe_enabled"] = bool(
        session.get("pre_import_probe_enabled", True)
    )
    result = adapter.retry_registration_import(
        session_id, post_registration
    )
    if not result.get("ok"):
        raise ctx.HTTPException(status_code=400, detail=result)
    return result


def retry_batch_import(ctx, batch_id):
    target = "chatgpt" if str(batch_id).startswith("batch_cgpt_") else "grok"
    adapter = ctx._get_registration_adapter(target)
    cfg = ctx.load_config()
    cfg["registration_target"] = target
    result = adapter.retry_registration_batch_import(
        batch_id, ctx._post_registration_config(cfg)
    )
    if not result.get("ok"):
        raise ctx.HTTPException(status_code=400, detail=result)
    return result


def manual_json_import(ctx, request):
    cfg = request.settings.model_dump()
    registration_target = str(cfg.get("registration_target") or "grok").lower()
    try:
        documents = list(request.payloads)
        if request.payload is not None:
            documents.append(request.payload)
        records: list[dict[str, Any]] = []
        for document in documents:
            records.extend(
                (
                    ctx._normalize_import_record_for_registration_target(
                        record, registration_target
                    )
                    for record in ctx._manual_import_records(document)
                )
            )
    except ValueError as exc:
        raise ctx.HTTPException(status_code=400, detail=str(exc)) from exc
    if not records:
        raise ctx.HTTPException(status_code=400, detail="JSON 中没有可导入账号")
    selected = str(
        cfg.get("registration_json_format") or cfg.get("auto_import_target") or "cpa"
    ).lower()
    cfg["auto_import_target"] = selected if selected in {"cpa", "sub2api"} else "cpa"
    pipeline = ctx._post_registration_config(cfg)
    from account_pipeline import import_account

    results: list[dict[str, Any]] = []
    adapter = ctx._get_registration_adapter()
    for record in records:
        adapter.wait_pipeline_stagger("import", pipeline.get("import_stagger_ms") or 0)
        response = import_account(record, pipeline)
        results.append(
            {
                "ok": bool(response.get("ok")),
                "status_code": response.get("status_code"),
                "error": str(response.get("error") or "")[:220] or None,
            }
        )
    imported = sum((1 for item in results if item["ok"]))
    failed = len(results) - imported
    return {
        "ok": failed == 0,
        "target": pipeline["target"],
        "count": len(results),
        "imported": imported,
        "failed": failed,
        "errors": [item["error"] for item in results if item.get("error")][:10],
    }
