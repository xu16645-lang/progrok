"""Extracted SSO CLI batch operations."""

from __future__ import annotations

from typing import Any

def load_sso_list(ctx, path, single):
    """Return list of (email_or_name, sso_cookie) tuples."""
    if single:
        return [("", single.strip())]
    if not path:
        return []
    out: list[tuple[str, str]] = []
    for line in ctx.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        email = ""
        if "----" in line:
            parts = line.split("----")
            email = parts[0].strip()
            line = parts[-1].strip()
        elif ":" in line and (not line.startswith("eyJ")):
            parts = line.rsplit(":", 1)
            email = parts[0].strip()
            line = parts[-1].strip()
        out.append((email, line))
    return out


def main(ctx):
    ap = ctx.argparse.ArgumentParser(
        description="SSO cookie → grok auth.json (纯 HTTP)"
    )
    ap.add_argument(
        "--sso",
        metavar="FILE",
        help="sso 列表文件（一行一个 JWT，或 邮箱----密码----sso）",
    )
    ap.add_argument("--sso-cookie", metavar="JWT", help="单个 sso cookie")
    ap.add_argument(
        "--out", default=None, help="输出 auth.json 路径（单账号或 --merge）"
    )
    ap.add_argument(
        "--out-dir",
        default=None,
        help="批量时每个账号写一个 {user_id}.json（可直接 cp 到 ~/.grok/auth.json）",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="合并到 --out，key 用 issuer::client_id::user_id",
    )
    ap.add_argument(
        "--into-project",
        action="store_true",
        default=True,
        help=f"默认导入到项目 auth.json: {ctx.AUTH_FILE}",
    )
    ap.add_argument(
        "--no-into-project",
        dest="into_project",
        action="store_false",
        help="不导入项目 auth.json，仅 --out / --out-dir 输出",
    )
    ap.add_argument("--delay", type=int, default=0, help="每个间隔秒数")
    ap.add_argument("--email", default="", help="写入 entry.email（可选）")
    args = ap.parse_args()
    cookies = ctx.load_sso_list(args.sso, args.sso_cookie)
    if not cookies:
        ap.error("需要 --sso 或 --sso-cookie")
    if (
        len(cookies) > 1
        and (not args.out_dir)
        and (not args.merge)
        and (not args.into_project)
    ):
        args.out_dir = args.out_dir or "./auth_out"
        print(f"批量模式默认 --out-dir {args.out_dir}")
    if (
        args.out is None
        and args.out_dir is None
        and (len(cookies) == 1)
        and (not args.into_project)
    ):
        args.out = str(ctx.Path.home() / ".grok" / "auth.json")
    target = (
        "项目 auth.json" if args.into_project else args.out or args.out_dir or "stdout"
    )
    print(
        f"🚀 SSO → auth.json: {len(cookies)} 个, target={target}, delay={args.delay}s"
    )
    ok = 0
    fail = 0


def process_one_sso(
    ctx, index, email_hint, sso, *, args_email, into_project, out_dir, out, merge, total
):
    """Process a single SSO cookie. Thread-safe for independent accounts."""
    result: dict[str, Any] = {
        "index": index,
        "email_hint": email_hint,
        "sso_hint": sso[:12] + "..." if len(sso) > 12 else "...",
    }
    try:
        token = ctx.sso_to_token(sso)
        if not token:
            result["status"] = "failed"
            result["error"] = "device flow failed or invalid sso"
            return result
        key, entry = ctx.token_to_auth_entry(token, email=args_email or email_hint)
        uid = entry.get("user_id") or ctx.secrets.token_hex(4)
        if out_dir:
            p = out_dir / f"{uid}.json"
            ctx.write_auth_json(p, key, entry)
            result["wrote"] = str(p)
        if out:
            if merge or total > 1:
                ctx.merge_auth_json(out, key, entry, unique=True)
                result["merged"] = str(out)
            else:
                ctx.write_auth_json(out, key, entry)
                result["wrote"] = str(out)
        if into_project:
            aid = ctx.import_into_project_auth(entry)
            result["imported_key"] = aid
        result["status"] = "ok"
        result["user_id"] = uid
        result["email"] = entry.get("email")
        return result
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        return result


def run_concurrent(
    ctx, cookies, *, max_workers, delay, args_email, into_project, out_dir, out, merge
):
    """Run SSO imports concurrently with per-item delay handled inside threads."""
    results: list[dict[str, Any]] = [None] * len(cookies)
    ok = 0
    fail = 0

    def _worker(args: tuple[int, str, str]) -> tuple[int, dict[str, Any]]:
        i, email_hint, sso = args
        if delay > 0 and i > 1:
            ctx.time.sleep(delay * (i - 1))
        res = ctx.process_one_sso(
            i,
            email_hint,
            sso,
            args_email=args_email,
            into_project=into_project,
            out_dir=out_dir,
            out=out,
            merge=merge,
            total=len(cookies),
        )
        print(f"\n{'=' * 60}\n[{i}/{len(cookies)}] {email_hint or ''}\n{'=' * 60}")
        for k, v in res.items():
            if k in ("index", "email_hint", "sso_hint"):
                continue
            if k == "status":
                mark = "✅" if v == "ok" else "❌"
                print(f"  {mark} [{i}] {v}")
            elif isinstance(v, str):
                print(f"  💾 {k}: {v}")
            else:
                print(f"  • {k}: {v}")
        return (i - 1, res)

    workers = min(max_workers, max(1, len(cookies)))
    with ctx.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sso-") as ex:
        for idx, res in ex.map(
            _worker, ((i, e, s) for i, (e, s) in enumerate(cookies, 1))
        ):
            results[idx] = res
            if res.get("status") == "ok":
                ok += 1
            else:
                fail += 1
    return (ok, fail, results)


def main(ctx):
    ap = ctx.argparse.ArgumentParser(
        description="SSO cookie → grok auth.json (纯 HTTP)"
    )
    ap.add_argument(
        "--sso",
        metavar="FILE",
        help="sso 列表文件（一行一个 JWT，或 邮箱----密码----sso）",
    )
    ap.add_argument("--sso-cookie", metavar="JWT", help="单个 sso cookie")
    ap.add_argument(
        "--out", default=None, help="输出 auth.json 路径（单账号或 --merge）"
    )
    ap.add_argument(
        "--out-dir",
        default=None,
        help="批量时每个账号写一个 {user_id}.json（可直接 cp 到 ~/.grok/auth.json）",
    )
    ap.add_argument(
        "--merge",
        action="store_true",
        help="合并到 --out，key 用 issuer::client_id::user_id",
    )
    ap.add_argument(
        "--into-project",
        action="store_true",
        default=True,
        help=f"默认导入到项目 auth.json: {ctx.AUTH_FILE}",
    )
    ap.add_argument(
        "--no-into-project",
        dest="into_project",
        action="store_false",
        help="不导入项目 auth.json，仅 --out / --out-dir 输出",
    )
    ap.add_argument("--delay", type=int, default=0, help="每个间隔秒数")
    ap.add_argument("--email", default="", help="写入 entry.email（可选）")
    ap.add_argument(
        "--threads",
        type=int,
        default=4,
        help="并发线程数（默认 4，最大 8；大量 SSO 时过高会冻 WSL）",
    )
    args = ap.parse_args()
    cookies = ctx.load_sso_list(args.sso, args.sso_cookie)
    if not cookies:
        ap.error("需要 --sso 或 --sso-cookie")
    threads = max(1, min(int(args.threads or 4), 8))
    if threads != args.threads:
        print(f"⚠️  threads {args.threads} → capped to {threads}")
    args.threads = threads
    if (
        len(cookies) > 1
        and (not args.out_dir)
        and (not args.merge)
        and (not args.into_project)
    ):
        args.out_dir = args.out_dir or "./auth_out"
        print(f"批量模式默认 --out-dir {args.out_dir}")
    if (
        args.out is None
        and args.out_dir is None
        and (len(cookies) == 1)
        and (not args.into_project)
    ):
        args.out = str(ctx.Path.home() / ".grok" / "auth.json")
    target = (
        "项目 auth.json" if args.into_project else args.out or args.out_dir or "stdout"
    )
    print(
        f"🚀 SSO → auth.json: {len(cookies)} 个, target={target}, delay={args.delay}s, threads={args.threads}"
    )
    ok, fail, results = ctx.run_concurrent(
        cookies,
        max_workers=args.threads,
        delay=args.delay,
        args_email=args.email,
        into_project=args.into_project,
        out_dir=ctx.Path(args.out_dir) if args.out_dir else None,
        out=ctx.Path(args.out) if args.out else None,
        merge=args.merge,
    )
    print(f"\n{'=' * 60}\n📊 完成: {ok}/{len(cookies)} 成功, {fail} 失败")
    return 0 if fail == 0 else 1
