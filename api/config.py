from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore")

    MODE: Literal["local", "hosted"] = "local"
    WORKSPACE_PATH: str = "."

    DATABASE_URL: str = ""
    # Direct (non-pooler) connection used only for the long-lived LISTEN/NOTIFY
    # socket. Supavisor recycles pooled sessions, which silently kills LISTEN;
    # a direct connection sidesteps that. Falls back to DATABASE_URL when unset.
    DIRECT_DATABASE_URL: str = ""
    SUPABASE_URL: str = ""
    SUPABASE_JWT_SECRET: str = ""
    VOYAGE_API_KEY: str = ""
    TURBOPUFFER_API_KEY: str = ""
    EMBEDDING_DIM: int = 512
    LOGFIRE_TOKEN: str = ""
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "us-east-1"
    S3_BUCKET: str = "supavault-documents"
    MISTRAL_API_KEY: str = ""
    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_AUTH_TOKEN: str = ""
    CLOUDFLARE_AI_GATEWAY_ID: str = ""
    QUIZ_GRADE_DAILY_LIMIT: int = Field(default=100, ge=1, le=10_000)
    PDF_BACKEND: str = "opendataloader"  # "opendataloader" or "mistral"
    STAGE: str = "dev"
    APP_URL: str = "http://localhost:3000"
    API_URL: str = "http://localhost:8000"
    # Comma-separated serialized origins. Override this with the ID shown by
    # chrome://extensions when using an unpacked development build.
    LOCAL_EXTENSION_ORIGINS: str = (
        "chrome-extension://dibilaenlekndomfbampadehjeahemha"
    )

    QUOTA_MAX_PAGES_PER_DOC: int = 300  # max pages per single document
    QUOTA_MAX_STORAGE_BYTES: int = 1_073_741_824  # 1 GB per user

    CONVERTER_URL: str = ""
    CONVERTER_SECRET: str = ""

    # 问答聊天窗（仅 local 模式）：OpenAI 兼容端点，未配置则聊天关闭
    # 真实 key 请放仓库根目录 .env（已 gitignore）或环境变量，不要写在这里
    LLM_BASE_URL: str = ""
    LLM_API_KEY: str = ""
    LLM_MODEL: str = ""
    LLM_TIMEOUT: float = 60.0
    LLM_PROTOCOL: str = "openai"  # "openai" (/chat/completions) or "anthropic" (/v1/messages)
    LLM_MAX_TOKENS: int = 8192  # anthropic protocol requires max_tokens

    # 自动摄入（仅 local 模式）：新源文档自动经两步 LLM 摄入生成 wiki 页面
    INGESTION_ENABLED: bool = False
    INGESTION_MAX_RETRIES: int = 3
    INGESTION_UPDATE_OVERVIEW: bool = True

    # 向量语义检索（仅 local 模式，可选）：关闭时检索行为与纯 FTS 完全一致。
    # EMBEDDING_BACKEND=api  —— 需配齐 EMBEDDING_BASE_URL/API_KEY/MODEL，
    #     独立于 LLM_*（聊天代理不一定有 embeddings 端点），SiliconFlow 有免费 BGE
    # EMBEDDING_BACKEND=local —— fastembed 本地跑 BGE（pip install fastembed），完全离线
    EMBEDDING_BACKEND: str = "api"  # "api" | "local"
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_MODEL: str = "BAAI/bge-large-zh-v1.5"
    LOCAL_EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"
    EMBEDDING_BATCH_SIZE: int = 32
    # 向量库（Qdrant）：qdrant（本地嵌入模式，零服务）/ qdrant-server（Docker 服务）
    VECTOR_BACKEND: str = "qdrant"
    QDRANT_URL: str = ""  # 仅 qdrant-server 需要，如 http://localhost:6333

    GLOBAL_OCR_ENABLED: bool = True
    GLOBAL_MAX_PAGES: int = 1_000_000
    GLOBAL_MAX_USERS: int = 10_000

    SENTRY_DSN: str = ""

    @model_validator(mode="after")
    def require_isolated_parser_for_hosted_uploads(self) -> "Settings":
        """Never let the hosted upload service fall back to local parsing.

        ``main.lifespan`` constructs S3 and OCR services when the access key
        and bucket are configured. Validate the matching condition while
        settings are loaded, before startup can initialize JWKS, Postgres, or
        any other network client.
        """
        hosted_uploads_enabled = bool(self.AWS_ACCESS_KEY_ID and self.S3_BUCKET)
        if (
            self.MODE == "hosted"
            and hosted_uploads_enabled
            and not self.CONVERTER_URL.strip()
        ):
            raise ValueError(
                "CONVERTER_URL is required when hosted uploads are enabled; "
                "the hosted API must not parse uploaded PDF or Office files in-process"
            )
        if (
            self.MODE == "hosted"
            and hosted_uploads_enabled
            and not self.CONVERTER_SECRET.strip()
        ):
            raise ValueError(
                "CONVERTER_SECRET is required when hosted uploads are enabled"
            )
        return self

    @property
    def listen_database_url(self) -> str:
        """Connection for the LISTEN loop — direct if configured, else the pooler."""
        return self.DIRECT_DATABASE_URL or self.DATABASE_URL

    @property
    def local_extension_origins(self) -> tuple[str, ...]:
        return tuple(
            origin.strip()
            for origin in self.LOCAL_EXTENSION_ORIGINS.split(",")
            if origin.strip()
        )


settings = Settings()
