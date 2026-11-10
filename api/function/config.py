"""Settings: .env + environment variables + in-memory console overrides.

Precedence (highest wins): console override > environment variable > default.
`ADMIN_TOKEN` is environment-only: it can never be overridden at runtime.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # api/
REPO_DIR = os.path.dirname(BASE_DIR)

DEFAULTS: Dict[str, str] = {
    "TELNYX_API_BASE": "https://api.telnyx.com/v2",
    "KV_BASE_URL": "https://api.telnyx.com/v2/storage/kvs",
    "API_URL": "http://localhost:8080",
    "ASSISTANT_MODEL": "moonshotai/Kimi-K2.6",
    "POLISH_MODEL": "zai-org/GLM-5.3-Flash",
    "CHAT_MODEL": "",
    # picks the next workflow edge in the console simulator: a small classification, so a fast model
    "ROUTER_MODEL": "zai-org/GLM-5.3-Flash",
    "LOG_LEVEL": "info",
    "VERIFY_SIGNATURES": "false",
    "SERVICE_NAME": "forge-api",
    "STORAGE_ENDPOINT": "",
    "STORAGE_BUCKET": "agent-forge",
    "STORAGE_REGION": "us-central-1",
    "STORAGE_ACCESS_KEY": "",
    "STORAGE_SECRET_KEY": "telnyx",
    "CLINIC_ASSISTANT_ID": "",
    "CLINIC_FRONT_DESK_NUMBER": "",
    "CLINIC_UTC_OFFSET": "-05:00",
    # "true" / "false"; empty = auto: required whenever API_URL is https:// (a public deployment)
    "REQUIRE_TOKENS": "",
    "FORGE_ASSISTANT_ID": "",
    # Flagged as blanks on purpose (a real deployment provides them):
    "TELNYX_API_KEY": "",
    "KV_NAMESPACE_ID": "",
    "STATE_URL": "",
    "STATE_TOKEN": "",
    "MCP_TOKEN": "",
    "TEXML_APP_ID": "",
    "FORGE_NUMBER": "",
    "TELNYX_PUBLIC_KEY": "",
    "FORGE_DRY_RUN": "",
}

# Console-overridable settings (ADMIN_TOKEN deliberately absent).
# secret: never shown or logged by the console.
OVERRIDABLE: Dict[str, Dict[str, Any]] = {
    "TELNYX_API_KEY": {"secret": True, "label": "Telnyx API key used for all v2 calls"},
    "MCP_TOKEN": {"secret": True, "label": "Bearer token the assistants use on webhook tools"},
    "STATE_URL": {"secret": False, "label": "forge-state RPC base URL"},
    "STATE_TOKEN": {"secret": True, "label": "Bearer token forge-api and alarms use on /rpc and /internal/jobs"},
    "KV_NAMESPACE_ID": {"secret": False, "label": "Telnyx KV storage namespace"},
    "API_URL": {"secret": False, "label": "Public base URL of this function (webhooks point back here)"},
    "TEXML_APP_ID": {"secret": False, "label": "TeXML application used for outbound AI calls"},
    "FORGE_NUMBER": {"secret": False, "label": "Phone number Forge calls from"},
    "ASSISTANT_MODEL": {"secret": False, "label": "Model for built assistants"},
    "POLISH_MODEL": {"secret": False, "label": "Model for persona polish"},
    "CHAT_MODEL": {"secret": False, "label": "Model for console chat (defaults to assistant model)"},
    "FORGE_DRY_RUN": {"secret": False, "label": "true = build agents in memory only"},
    "STORAGE_ENDPOINT": {"secret": False, "label": "S3-compatible endpoint (Telnyx Cloud Storage)"},
    "STORAGE_BUCKET": {"secret": False, "label": "Bucket for archives"},
    "STORAGE_REGION": {"secret": False, "label": "Bucket region"},
    "STORAGE_ACCESS_KEY": {"secret": True, "label": "S3 access key (defaults to the API key)"},
    "STORAGE_SECRET_KEY": {"secret": True, "label": "S3 secret key"},
    "CLINIC_ASSISTANT_ID": {"secret": False, "label": "Clara's assistant id (Forge hands off to it)"},
    "CLINIC_FRONT_DESK_NUMBER": {"secret": False, "label": "Clinic front desk number for transfers"},
    "FORGE_ASSISTANT_ID": {"secret": False, "label": "Forge's assistant id (browser voice calls)"},
}


def _to_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def lookup(name: str, overrides: Optional[Dict[str, str]]) -> Tuple[str, str]:
    """Return (value, source) with source in override | env | default | unset."""
    if overrides and name in overrides and overrides[name] not in (None, ""):
        return str(overrides[name]), "override"
    if name in os.environ and os.environ[name] != "":
        return os.environ[name], "env"
    if name in DEFAULTS and DEFAULTS[name] != "":
        return DEFAULTS[name], "default"
    if name in DEFAULTS:
        return "", "default"
    return "", "unset"


@dataclass(frozen=True)
class Settings:
    telnyx_api_key: str
    telnyx_api_base: str
    kv_namespace_id: str
    kv_base_url: str
    state_url: str
    state_token: str
    mcp_token: str
    admin_token: str
    public_api_url: str
    texml_app_id: str
    forge_number: str
    assistant_model: str
    polish_model: str
    chat_model: str
    dry_run: bool
    log_level: str
    verify_signatures: bool
    telnyx_public_key: str
    service_name: str
    storage_endpoint: str
    storage_bucket: str
    storage_region: str
    storage_access_key: str
    storage_secret_key: str
    clinic_assistant_id: str
    clinic_front_desk_number: str
    clinic_utc_offset: str
    require_tokens: bool = False
    router_model: str = ""
    forge_assistant_id: str = ""

    @property
    def local_kv(self) -> bool:
        return not (self.kv_namespace_id and self.telnyx_api_key)

    @property
    def local_state(self) -> bool:
        return not self.state_url

    @property
    def local_storage(self) -> bool:
        return not (self.storage_endpoint and self.storage_access_key)


def load_dotenv(path: str) -> None:
    """Load KEY=VALUE lines from a .env file; existing environment variables win."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings(overrides: Optional[Dict[str, str]] = None) -> Settings:
    def val(name: str) -> Tuple[str, str]:
        return lookup(name, overrides)

    telnyx_api_key, _ = val("TELNYX_API_KEY")
    state_url, _ = val("STATE_URL")
    assistant_model, _ = val("ASSISTANT_MODEL")
    chat_model, _ = val("CHAT_MODEL")
    storage_endpoint, _ = val("STORAGE_ENDPOINT")
    storage_access_key, _ = val("STORAGE_ACCESS_KEY")
    dry_run_flag, _ = val("FORGE_DRY_RUN")

    settings = Settings(
        telnyx_api_key=telnyx_api_key,
        telnyx_api_base=val("TELNYX_API_BASE")[0],
        kv_namespace_id=val("KV_NAMESPACE_ID")[0],
        kv_base_url=val("KV_BASE_URL")[0],
        state_url=state_url.rstrip("/"),
        state_token=val("STATE_TOKEN")[0],
        mcp_token=val("MCP_TOKEN")[0],
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
        public_api_url=val("API_URL")[0].rstrip("/"),
        texml_app_id=val("TEXML_APP_ID")[0],
        forge_number=val("FORGE_NUMBER")[0],
        assistant_model=assistant_model,
        polish_model=val("POLISH_MODEL")[0],
        chat_model=chat_model or assistant_model,
        dry_run=_to_bool(dry_run_flag) or not telnyx_api_key,
        log_level=val("LOG_LEVEL")[0],
        verify_signatures=_to_bool(val("VERIFY_SIGNATURES")[0]),
        telnyx_public_key=val("TELNYX_PUBLIC_KEY")[0],
        service_name=val("SERVICE_NAME")[0],
        storage_endpoint=storage_endpoint.rstrip("/"),
        storage_bucket=val("STORAGE_BUCKET")[0],
        storage_region=val("STORAGE_REGION")[0],
        storage_access_key=storage_access_key or telnyx_api_key,
        storage_secret_key=val("STORAGE_SECRET_KEY")[0],
        clinic_assistant_id=val("CLINIC_ASSISTANT_ID")[0],
        clinic_front_desk_number=val("CLINIC_FRONT_DESK_NUMBER")[0],
        clinic_utc_offset=val("CLINIC_UTC_OFFSET")[0],
        require_tokens=_require_tokens(val("REQUIRE_TOKENS")[0], val("API_URL")[0]),
        forge_assistant_id=val("FORGE_ASSISTANT_ID")[0],
        router_model=val("ROUTER_MODEL")[0] or chat_model or assistant_model,
    )
    return settings


def _require_tokens(flag: str, api_url: str) -> bool:
    """Fail closed on a public deployment: a missing token must refuse requests, never open the route."""
    if str(flag).strip().lower() in ("true", "false"):
        return _to_bool(flag)
    return str(api_url).startswith("https://")


def normalize_phone(raw: Any) -> Optional[str]:
    """Normalise a phone-ish value to E.164, or None if it isn't a phone."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if value.lower().startswith("sip:"):
        value = value[4:]
    if "@" in value:
        value = value.split("@", 1)[0]
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) == 10:
        return "+1" + digits
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return None


def mask_phone(phone: Optional[str]) -> str:
    if not phone:
        return ""
    value = str(phone)
    if len(value) <= 7:
        return "***"
    return value[:3] + "***" + value[-4:]


def mask_secret(secret: Optional[str]) -> str:
    if not secret:
        return ""
    tail = secret[-4:] if len(secret) >= 4 else ""
    return "******" + tail


def settings_sources(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """source per OVERRIDABLE name (values handled separately)."""
    result: Dict[str, str] = {}
    for name in OVERRIDABLE:
        _, source = lookup(name, overrides)
        result[name] = source
    return result


def override_values(settings: Settings, overrides: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Effective values for every overridable name."""
    result: Dict[str, str] = {}
    mapping = {
        "TELNYX_API_KEY": settings.telnyx_api_key,
        "MCP_TOKEN": settings.mcp_token,
        "STATE_URL": settings.state_url,
        "STATE_TOKEN": settings.state_token,
        "KV_NAMESPACE_ID": settings.kv_namespace_id,
        "API_URL": settings.public_api_url,
        "TEXML_APP_ID": settings.texml_app_id,
        "FORGE_NUMBER": settings.forge_number,
        "ASSISTANT_MODEL": settings.assistant_model,
        "POLISH_MODEL": settings.polish_model,
        "CHAT_MODEL": settings.chat_model,
        "FORGE_DRY_RUN": "true" if settings.dry_run else "false",
        "STORAGE_ENDPOINT": settings.storage_endpoint,
        "STORAGE_BUCKET": settings.storage_bucket,
        "STORAGE_REGION": settings.storage_region,
        "STORAGE_ACCESS_KEY": settings.storage_access_key,
        "STORAGE_SECRET_KEY": settings.storage_secret_key,
        "CLINIC_ASSISTANT_ID": settings.clinic_assistant_id,
        "CLINIC_FRONT_DESK_NUMBER": settings.clinic_front_desk_number,
        "FORGE_ASSISTANT_ID": settings.forge_assistant_id,
    }
    for name, value in mapping.items():
        result[name] = "" if value is None else str(value)
    return result


DEMO_PREFIX = "+1555019"  # public console chats get +1 555 019 xxxx (fictional range, distinct from test numbers)


def is_demo_number(phone: Optional[str]) -> bool:
    return bool(phone) and str(phone).startswith(DEMO_PREFIX)
