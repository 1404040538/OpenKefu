import argparse
import os
import socket
import sys
from pathlib import Path

import uvicorn

from openkefu.platforms.pdd.config import PROJECT_ROOT
from openkefu.web.config import load_config


def check_port_free(host: str, port: int) -> None:
    """启动前预检端口：被占用时给出可操作的报错（常见于 uvicorn reload
    的残留子进程或上一个未退出的实例）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            print(f"[startup] 端口 {host}:{port} 已被占用（{exc}）。", file=sys.stderr)
            print(
                "[startup] 可能是上一次运行的残留进程（dev 模式 uvicorn reload 的"
                "子进程不受父进程退出影响）。请排查：", file=sys.stderr)
            print(f"[startup]   netstat -ano | findstr :{port}", file=sys.stderr)
            print("[startup]   taskkill /PID <PID> /F 后重试", file=sys.stderr)
            raise SystemExit(1)


def frontend_build_issue(frontend_dist: str, *, project_root: Path = PROJECT_ROOT) -> str | None:
    dist = Path(frontend_dist)
    if not dist.is_absolute():
        dist = project_root / dist
    index = dist / "index.html"
    if not index.is_file():
        return f"frontend build is missing: {index}"

    web_root = project_root / "web"
    source_files = [
        web_root / "package.json",
        web_root / "package-lock.json",
        web_root / "vite.config.ts",
        web_root / "postcss.config.js",
        web_root / "tailwind.config.js",
    ]
    source_dir = web_root / "src"
    if source_dir.is_dir():
        source_files.extend(path for path in source_dir.rglob("*") if path.is_file())
    newest_source = max((path.stat().st_mtime for path in source_files if path.is_file()), default=0.0)
    if newest_source > index.stat().st_mtime:
        return f"frontend build is stale: {index} is older than web source files"
    return None


def main():
    parser = argparse.ArgumentParser(description="Start the PDD Agent web service.")
    parser.add_argument("--mode", choices=("dev", "test", "prod"), help="startup mode")
    parser.add_argument("--test", action="store_true", help="start in test mode")
    parser.add_argument("--prod", action="store_true", help="start in production mode")
    parser.add_argument("--role", choices=("api", "worker", "both"), help="runtime role")
    parser.add_argument("--host", help="bind host, overrides config.local.json")
    parser.add_argument("--port", type=int, help="bind port, overrides config.local.json")
    args = parser.parse_args()

    mode_flags = [value for value in (args.mode, "test" if args.test else None, "prod" if args.prod else None) if value]
    if len(set(mode_flags)) > 1:
        raise SystemExit("--mode, --test, and --prod specify conflicting startup modes")
    mode = mode_flags[0] if mode_flags else "dev"
    prod_like_mode = mode in {"test", "prod"}

    if args.role:
        os.environ["OPENKEFU_RUNTIME_ROLE"] = args.role
    if args.host:
        os.environ["OPENKEFU_SERVER_HOST"] = args.host
    if args.port is not None:
        os.environ["OPENKEFU_SERVER_PORT"] = str(args.port)
    if prod_like_mode:
        os.environ.pop("OPENKEFU_DEV", None)
        # 安全默认：仅绑定本机回环地址。如需公网/局域网访问，必须显式 --host 0.0.0.0
        # 并自行配置 HTTPS 反向代理与访问控制。
        os.environ.setdefault("OPENKEFU_SERVER_HOST", "127.0.0.1")
        if mode == "test":
            os.environ.setdefault("OPENKEFU_SERVER_PORT", "18000")
    else:
        os.environ["OPENKEFU_DEV"] = "1"
        # 注意：OPENKEFU_DEV 现在只控制 dev 模式端口(8001)/reload/前端代理，
        # 不再切换数据库——统一连接 config.local.json 的 mysql.database (openkefu)

    config = load_config()
    if args.port is not None:
        port = args.port
    elif prod_like_mode:
        port = config.server.port
    else:
        port = int(os.getenv("OPENKEFU_DEV_BACKEND_PORT", "8001"))

    check_port_free(config.server.host, port)
    role = os.environ.get("OPENKEFU_RUNTIME_ROLE") or config.runtime.role
    print(
        f"[startup] mode={mode} role={role} pid={os.getpid()} "
        f"binding={config.server.host}:{port} db={config.mysql.database} "
        f"redis_db={'-'} lease_ttl={config.runtime.lease_ttl_seconds}s"
    )

    if prod_like_mode:
        build_issue = frontend_build_issue(config.server.frontend_dist)
        if build_issue:
            message = f"{build_issue}; run `cd web; npm ci; npm run build`"
            if mode == "prod":
                raise SystemExit(message)
            print(f"[test] warning: {message}")
        print(f"[{mode}] serving web/dist on {config.server.host}:{port}")
    else:
        print(
            f"[dev] backend API on {config.server.host}:{port}; "
            f"database={config.mysql.database}; run npm run dev in web/"
        )

    uvicorn.run(
        "openkefu.web.app:create_app",
        host=config.server.host,
        port=port,
        factory=True,
        reload=not prod_like_mode,
    )


if __name__ == "__main__":
    main()
