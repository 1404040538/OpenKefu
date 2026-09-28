from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openkefu.platforms.pdd.config import PROJECT_ROOT


CONFIG_PATH = PROJECT_ROOT / "config.local.json"


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    frontend_dist: str


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"
    pool_size: int = 8
    ssl_ca: str = ""
    ssl_cert: str = ""
    ssl_key: str = ""
    ssl_verify_identity: bool = True


@dataclass(frozen=True)
class RedisConfig:
    url: str


@dataclass(frozen=True)
class RuntimeConfig:
    role: str
    worker_id: str
    lease_ttl_seconds: int
    reply_workers: int
    command_secret: str


@dataclass(frozen=True)
class SecurityConfig:
    jwt_secret: str
    jwt_expire_minutes: int
    data_encryption_key: str
    allowed_origins: tuple[str, ...]
    rate_limit_disabled: bool


@dataclass(frozen=True)
class LoggingConfig:
    path: str
    level: str


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class EmbeddingConfig:
    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class VectorStoreConfig:
    provider: str
    path: str
    collection: str


@dataclass(frozen=True)
class StorageConfig:
    knowledge_dir: str


@dataclass(frozen=True)
class ServerStatusConfig:
    enabled: bool
    collect_interval_seconds: int
    persist_interval_seconds: int
    raw_retention_days: int
    rollup_retention_days: int
    top_process_limit: int


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    mysql: MySQLConfig
    redis: RedisConfig
    runtime: RuntimeConfig
    security: SecurityConfig
    logging: LoggingConfig
    llm: LLMConfig
    embedding: EmbeddingConfig
    vector_store: VectorStoreConfig
    storage: StorageConfig
    server_status: ServerStatusConfig


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise RuntimeError(f"config.local.json missing section: {name}")
    return value


def _optional_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


WEAK_SECRET_VALUES = {
    "change-me",
    "change_me",
    "change-me-before-use",
    "changeme",
    "default",
    "jwt-secret",
    "secret",
    "password",
    "please-change-me",
    "replace-me",
    "replace-with-a-random-secret",
}


def _resolve_secret(
    *,
    value: str,
    label: str,
    min_length: int = 32,
) -> str:
    value = str(value or "").strip()
    normalized = value.lower()
    if not value:
        raise RuntimeError(f"{label} is required")
    if normalized in WEAK_SECRET_VALUES or "change-me" in normalized or "replace-me" in normalized:
        raise RuntimeError(f"{label} uses a default/example value; generate a unique random secret")
    if len(value) < min_length:
        raise RuntimeError(f"{label} must be at least {min_length} characters")
    if len(set(value)) < 8:
        raise RuntimeError(f"{label} has too little character diversity")
    return value


def _validate_redis_security(redis_url: str, runtime_role: str) -> None:
    if runtime_role == "both":
        return
    parsed = urlparse(redis_url)
    if parsed.scheme not in {"redis", "rediss"}:
        raise RuntimeError("redis.url must use redis:// or rediss://")
    if not parsed.password:
        raise RuntimeError("redis.url must include ACL/password when runtime.role is api or worker")
    password = str(parsed.password)
    normalized = password.lower()
    if (
        len(password) < 16
        or normalized in WEAK_SECRET_VALUES
        or "change-me" in normalized
        or "change_me" in normalized
        or "replace" in normalized
    ):
        raise RuntimeError("redis.url password must be a unique secret of at least 16 characters")


def _is_loopback_host(host: str) -> bool:
    return str(host or "").strip().lower() in {"127.0.0.1", "::1", "localhost"}


def load_config(path: Path | None = None) -> AppConfig:
    path = path or CONFIG_PATH
    if not path.exists():
        raise RuntimeError(
            f"missing config file: {path}. "
            "copy config.example.json to config.local.json, then set your "
            "MySQL, security secret, and LLM settings."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    server = _section(data, "server")
    mysql = _section(data, "mysql")
    redis = _optional_section(data, "redis")
    runtime = _optional_section(data, "runtime")
    security = _section(data, "security")
    logging = _section(data, "logging")
    llm = _optional_section(data, "llm")
    embedding = _optional_section(data, "embedding")
    vector_store = _optional_section(data, "vector_store")
    storage = _optional_section(data, "storage")
    server_status = _optional_section(data, "server_status")

    llm_api_key = str(llm.get("api_key") or os.getenv("OPENKEFU_LLM_API_KEY") or "")
    llm_base_url = str(llm.get("base_url") or os.getenv("OPENKEFU_LLM_BASE_URL") or "https://api.deepseek.com")
    llm_model = str(llm.get("model") or os.getenv("OPENKEFU_LLM_MODEL") or "deepseek-v4-flash")
    embedding_api_key = str(embedding.get("api_key") or os.getenv("OPENKEFU_EMBEDDING_API_KEY") or "")
    embedding_base_url = str(embedding.get("base_url") or os.getenv("OPENKEFU_EMBEDDING_BASE_URL") or "")
    embedding_model = str(embedding.get("model") or os.getenv("OPENKEFU_EMBEDDING_MODEL") or "")
    if not llm_api_key and embedding_api_key and embedding_model.startswith("deepseek-"):
        llm_api_key = embedding_api_key
        llm_base_url = embedding_base_url or llm_base_url
        llm_model = embedding_model
    # 统一数据库：固定使用 mysql.database（config.local.json 默认 openkefu），
    # 不再区分 dev/prod 两套库
    mysql_database = str(mysql.get("database") or "openkefu")
    mysql_host = str(mysql.get("host") or "127.0.0.1")
    mysql_ssl_ca = str(mysql.get("ssl_ca") or os.getenv("OPENKEFU_MYSQL_SSL_CA") or "").strip()
    if not _is_loopback_host(mysql_host) and not mysql_ssl_ca:
        raise RuntimeError("remote mysql.host requires mysql.ssl_ca for certificate-verified TLS")
    vector_store_provider = str(vector_store.get("provider") or "mysql").strip().lower()
    # Chroma was previously embedded locally. Treat the legacy setting as a
    # migration alias so existing private configs move to MySQL automatically.
    if vector_store_provider == "chroma":
        vector_store_provider = "mysql"
    if vector_store_provider != "mysql":
        raise RuntimeError("vector_store.provider must be mysql")
    runtime_role = str(os.getenv("OPENKEFU_RUNTIME_ROLE") or runtime.get("role") or "both").lower()
    if runtime_role not in {"api", "worker", "both"}:
        raise RuntimeError("runtime.role must be one of: api, worker, both")
    # 密钥优先取环境变量，其次取 config.local.json；二者均未配置时直接报错，
    # 杜绝“源码硬编码 + 所有部署共享同一密钥”的问题。
    jwt_secret = _resolve_secret(
        value=os.getenv("OPENKEFU_JWT_SECRET") or str(security.get("jwt_secret") or ""),
        label="security.jwt_secret",
    )
    runtime_command_secret = _resolve_secret(
        value=os.getenv("OPENKEFU_RUNTIME_COMMAND_SECRET") or str(runtime.get("command_secret") or ""),
        label="runtime.command_secret",
    )
    data_encryption_key = _resolve_secret(
        value=os.getenv("OPENKEFU_DATA_ENCRYPTION_KEY") or str(security.get("data_encryption_key") or ""),
        label="security.data_encryption_key",
    )
    redis_url = str(redis.get("url") or os.getenv("OPENKEFU_REDIS_URL") or "redis://127.0.0.1:6379/0")
    _validate_redis_security(redis_url, runtime_role)

    allowed_origins = tuple(
        str(item).strip()
        for item in (security.get("allowed_origins") or [])
        if str(item).strip()
    )
    for origin in allowed_origins:
        parsed_origin = urlparse(origin)
        if (
            origin == "*"
            or parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.netloc
            or parsed_origin.path
            or parsed_origin.params
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise RuntimeError("security.allowed_origins must contain exact http(s) origins; wildcard is forbidden")
    # 测试环境可禁用限流（OPENKEFU_RATE_LIMIT_DISABLED=1 或 security.rate_limit_disabled=true）；
    # 生产环境保持默认开启。
    rate_limit_disabled = (
        str(os.getenv("OPENKEFU_RATE_LIMIT_DISABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
        or _bool(security.get("rate_limit_disabled"), False)
    )

    return AppConfig(
        server=ServerConfig(
            host=str(os.getenv("OPENKEFU_SERVER_HOST") or server.get("host") or "127.0.0.1"),
            port=int(os.getenv("OPENKEFU_SERVER_PORT") or server.get("port") or 8000),
            frontend_dist=str(server.get("frontend_dist") or "web/dist"),
        ),
        mysql=MySQLConfig(
            host=mysql_host,
            port=int(mysql.get("port") or 3306),
            user=str(mysql.get("user") or "root"),
            password=str(mysql.get("password") or ""),
            database=mysql_database,
            charset=str(mysql.get("charset") or "utf8mb4"),
            pool_size=max(1, int(mysql.get("pool_size") or os.getenv("OPENKEFU_MYSQL_POOL_SIZE") or 8)),
            ssl_ca=mysql_ssl_ca,
            ssl_cert=str(mysql.get("ssl_cert") or os.getenv("OPENKEFU_MYSQL_SSL_CERT") or "").strip(),
            ssl_key=str(mysql.get("ssl_key") or os.getenv("OPENKEFU_MYSQL_SSL_KEY") or "").strip(),
            ssl_verify_identity=_bool(mysql.get("ssl_verify_identity"), True),
        ),
        redis=RedisConfig(
            url=redis_url,
        ),
        runtime=RuntimeConfig(
            role=runtime_role,
            worker_id=str(os.getenv("OPENKEFU_RUNTIME_WORKER_ID") or runtime.get("worker_id") or ""),
            lease_ttl_seconds=max(10, int(runtime.get("lease_ttl_seconds") or 45)),
            reply_workers=max(1, int(runtime.get("reply_workers") or 16)),
            command_secret=runtime_command_secret,
        ),
        security=SecurityConfig(
            jwt_secret=jwt_secret,
            jwt_expire_minutes=int(security.get("jwt_expire_minutes") or 10080),
            data_encryption_key=data_encryption_key,
            allowed_origins=allowed_origins,
            rate_limit_disabled=rate_limit_disabled,
        ),
        logging=LoggingConfig(
            path=str(logging.get("path") or "logs/openkefu_web.log"),
            level=str(logging.get("level") or "INFO"),
        ),
        llm=LLMConfig(
            api_key=llm_api_key,
            base_url=llm_base_url,
            model=llm_model,
        ),
        embedding=EmbeddingConfig(
            api_key=embedding_api_key,
            base_url=embedding_base_url,
            model=embedding_model,
        ),
        vector_store=VectorStoreConfig(
            provider=vector_store_provider,
            path=str(vector_store.get("path") or ""),
            collection=str(vector_store.get("collection") or "openkefu_knowledge"),
        ),
        storage=StorageConfig(
            knowledge_dir=str(storage.get("knowledge_dir") or "data/knowledge_files"),
        ),
        server_status=ServerStatusConfig(
            enabled=_bool(server_status.get("enabled"), True),
            collect_interval_seconds=max(2, int(server_status.get("collect_interval_seconds") or 5)),
            persist_interval_seconds=max(10, int(server_status.get("persist_interval_seconds") or 30)),
            raw_retention_days=max(1, int(server_status.get("raw_retention_days") or 7)),
            rollup_retention_days=max(1, int(server_status.get("rollup_retention_days") or 30)),
            top_process_limit=max(1, int(server_status.get("top_process_limit") or 8)),
        ),
    )
