# -*- coding: utf-8 -*-
"""
多环境与鉴权配置管理器
支持多环境隔离、标签打标、备注说明、密钥脱敏保护及防覆盖机制
"""

import json
import math
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager, suppress

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback keeps process-local safety
    fcntl = None

_DEFAULT_CONFIG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "config.json")
)
CONFIG_PATH = os.path.abspath(
    os.environ.get("TIM_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
)

_config_lock = threading.RLock()
_config_file_lock_state = threading.local()


def _open_config_lock_file(lock_path: str):
    try:
        return open(lock_path, "a+", encoding="utf-8")  # noqa: PTH123
    except OSError as exc:
        raise RuntimeError(f"无法打开配置文件锁: {lock_path}") from exc


@contextmanager
def _config_file_lock():
    """Serialize config transactions across worker processes on POSIX hosts."""
    if fcntl is None:
        yield
        return

    depth = getattr(_config_file_lock_state, "depth", 0)
    if depth:
        _config_file_lock_state.depth = depth + 1
        try:
            yield
        finally:
            _config_file_lock_state.depth = depth
        return

    lock_path = f"{CONFIG_PATH}.lock"
    with _open_config_lock_file(lock_path) as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise RuntimeError(f"无法获取配置文件锁: {lock_path}") from exc

        _config_file_lock_state.depth = 1
        try:
            yield
        finally:
            _config_file_lock_state.depth = 0
            with suppress(OSError):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _config_transaction():
    """Hold both process-local and cross-process config locks."""
    with _config_lock, _config_file_lock():
        yield


DEFAULT_ENV = {
    "id": "env_default",
    "name": "示例开发环境",
    "tag": "DEV",
    "remark": "请在「环境配置」中填入腾讯云控制台对应的 SDKAppID 和 SecretKey，或腾讯云 API 3.0 密钥",
    "sdkappid": 1400000000,
    "secret_key": "YOUR_SECRET_KEY_HERE",
    "admin_user": "administrator",
    "domain": "console.tim.qq.com",
    "tc_secret_id": "",
    "tc_secret_key": "",
    "tc_region": "ap-guangzhou",
    "tc_endpoint": "live.tencentcloudapi.com",
    "live_push_domain": "",
    "live_play_domain": "",
    "live_push_key": "",
    "live_play_key": "",
    "delay_sec": 0.05,
}

MAX_DELAY_SEC = 24 * 60 * 60


SECRET_FIELDS = ["secret_key", "tc_secret_key", "live_push_key", "live_play_key"]


def validate_delay_sec(value, *, default: float | None = None) -> float:
    """Validate the inter-request delay used by configuration and batch jobs."""
    candidate = default if value is None and default is not None else value
    if isinstance(candidate, bool):
        raise ValueError("delay_sec 必须是有限数值")
    try:
        delay = float(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError("delay_sec 必须是有限数值") from exc
    if not math.isfinite(delay):
        raise ValueError("delay_sec 不允许 NaN 或 Infinity")
    if delay < 0 or delay > MAX_DELAY_SEC:
        raise ValueError(f"delay_sec 必须在 0 到 {MAX_DELAY_SEC} 秒之间")
    return delay


def is_field_sensitive(field_name: str) -> bool:
    """按命名语义动态识别敏感字段，自动施加脱敏与防覆盖保护"""
    name = str(field_name).lower()
    if name in SECRET_FIELDS:
        return True
    return any(name.endswith(suffix) for suffix in ["_key", "_secret", "_token", "_password", "secretkey"])


def mask_secret(secret: str) -> str:
    """密钥脱敏处理：保留前4位和后4位，中间星号脱敏"""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "******"
    return secret[:4] + "*" * min(32, len(secret) - 8) + secret[-4:]


def is_secret_masked(secret: str) -> bool:
    """检查是否为脱敏字符串，用于防止前端提交的脱敏掩码覆盖真实密钥"""
    if not secret:
        return False
    return "*" in secret or "•" in secret or secret.startswith("...")


def load_raw_config() -> dict:
    """读取未经处理的原始配置"""
    with _config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[WARN] Failed to load config.json: {exc}")
        return {}


def load_config() -> dict:
    """读取配置并格式化多环境，向下兼容根层级字段"""
    raw = load_raw_config()
    environments = raw.get("environments", [])

    # 如果历史配置文件中没有 environments 列表，则自动迁移升级
    if not environments or not isinstance(environments, list):
        initial_env = dict(DEFAULT_ENV)
        if "sdkappid" in raw and raw["sdkappid"]:
            initial_env["sdkappid"] = raw["sdkappid"]
        if "secret_key" in raw and raw["secret_key"]:
            initial_env["secret_key"] = raw["secret_key"]
        if "admin_user" in raw and raw["admin_user"]:
            initial_env["admin_user"] = raw["admin_user"]
        if "domain" in raw and raw["domain"]:
            initial_env["domain"] = raw["domain"]
        if "delay_sec" in raw:
            initial_env["delay_sec"] = validate_delay_sec(
                raw["delay_sec"],
                default=DEFAULT_ENV["delay_sec"],
            )
        environments = [initial_env]

    active_id = raw.get("active_env_id")
    active_env = next((e for e in environments if e.get("id") == active_id), None)
    if not active_env:
        active_env = environments[0]
        active_id = active_env.get("id", "env_default")

    # 构建并同步根配置 (动态继承当前环境全部专有配置，无损透传新业务属性)
    cfg = dict(active_env)
    cfg["active_env_id"] = active_id
    cfg["environments"] = environments

    # 兜底注入默认基础字段，确保老业务与测试套件完全向后兼容
    for k, v in DEFAULT_ENV.items():
        if k not in cfg:
            cfg[k] = v
    cfg["delay_sec"] = validate_delay_sec(
        cfg.get("delay_sec"),
        default=DEFAULT_ENV["delay_sec"],
    )
    return cfg


def _write_config_unlocked(new_config: dict) -> dict:
    """Write config atomically; callers hold the process/file transaction locks."""
    disk_data = {
        "active_env_id": new_config.get("active_env_id", "env_default"),
        "environments": new_config.get("environments", []),
    }
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(CONFIG_PATH),
            prefix=f".{os.path.basename(CONFIG_PATH)}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(disk_data, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, CONFIG_PATH)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            with suppress(OSError):
                os.remove(temp_path)
    return new_config


def save_config(new_config: dict) -> dict:
    """持久化保存配置：进程锁 + 跨进程文件锁 + 原子替换。"""
    with _config_transaction():
        environments = new_config.get("environments", [])
        if not isinstance(environments, list):
            raise ValueError("environments 必须是列表")
        for environment in environments:
            if not isinstance(environment, dict):
                raise ValueError("环境配置必须是对象")
            environment["delay_sec"] = validate_delay_sec(
                environment.get("delay_sec"),
                default=DEFAULT_ENV["delay_sec"],
            )
        return _write_config_unlocked(new_config)


def get_environments_payload(mask: bool = True) -> dict:
    """获取所有环境列表与当前活动环境（支持密钥脱敏与泛化安全检查）"""
    cfg = load_config()
    envs = []
    for e in cfg["environments"]:
        item = dict(e)
        # 兼容老版固定字段布尔标识
        item["has_secret"] = bool(item.get("secret_key"))
        item["has_tc_secret"] = bool(item.get("tc_secret_key"))
        item["has_live_push_key"] = bool(item.get("live_push_key"))
        item["has_live_play_key"] = bool(item.get("live_play_key"))

        # 动态对所有敏感字段进行脱敏
        for k, v in list(item.items()):
            if is_field_sensitive(k) and isinstance(v, str):
                item[f"has_{k}"] = bool(v)
                if mask and v:
                    item[k] = mask_secret(v)
        envs.append(item)

    active_env = next((e for e in envs if e.get("id") == cfg["active_env_id"]), envs[0] if envs else None)
    return {
        "active_env_id": cfg["active_env_id"],
        "active_env": active_env,
        "environments": envs,
    }


def switch_environment(env_id: str) -> dict:
    """切换当前活动环境"""
    with _config_transaction():
        cfg = load_config()
        target_env = next((e for e in cfg["environments"] if e.get("id") == env_id), None)
        if not target_env:
            raise ValueError(f"环境 ID {env_id} 不存在")

        cfg["active_env_id"] = env_id
        _write_config_unlocked(cfg)
        return get_environments_payload(mask=True)


def save_environment(env_data: dict) -> dict:
    """
    新建或更新单项环境配置
    关键安全特性：如果提交的敏感密钥为空或为脱敏掩码（包含星号），则保留原真实密钥不变！
    全面支持动态扩展属性，杜绝新业务字段被覆盖或丢弃。
    """
    with _config_transaction():
        cfg = load_config()
        env_id = env_data.get("id")
        is_new = False

        if not env_id:
            env_id = f"env_{uuid.uuid4().hex[:8]}"
            env_data["id"] = env_id
            is_new = True

        existing_idx = next((i for i, e in enumerate(cfg["environments"]) if e.get("id") == env_id), -1)

        if existing_idx >= 0:
            existing_env = cfg["environments"][existing_idx]
            env_data["delay_sec"] = validate_delay_sec(
                env_data.get("delay_sec", existing_env.get("delay_sec")),
                default=DEFAULT_ENV["delay_sec"],
            )
            all_sec_keys = {k for k in list(existing_env.keys()) + list(env_data.keys()) if is_field_sensitive(k)}
            for sec_field in all_sec_keys:
                submitted_val = str(env_data.get(sec_field) or "").strip()
                if not submitted_val or is_secret_masked(submitted_val):
                    env_data[sec_field] = existing_env.get(sec_field, "")
                else:
                    env_data[sec_field] = submitted_val

            cfg["environments"][existing_idx] = {
                **existing_env,
                **env_data,
            }
        else:
            env_data["delay_sec"] = validate_delay_sec(
                env_data.get("delay_sec"),
                default=DEFAULT_ENV["delay_sec"],
            )
            for key in list(env_data.keys()):
                if is_field_sensitive(key):
                    submitted_val = str(env_data.get(key) or "").strip()
                    if is_secret_masked(submitted_val):
                        env_data[key] = ""
            cfg["environments"].append(env_data)

        if is_new and len(cfg["environments"]) == 1:
            cfg["active_env_id"] = env_id

        _write_config_unlocked(cfg)
        payload = get_environments_payload(mask=True)
        saved_env = next((e for e in payload["environments"] if e.get("id") == env_id), None)
        return {
            **payload,
            "saved_env": saved_env,
            "saved_id": env_id,
        }


def delete_environment(env_id: str) -> dict:
    """删除环境（至少保留一个环境）"""
    with _config_transaction():
        cfg = load_config()
        if len(cfg["environments"]) <= 1:
            raise ValueError("至少需要保留一个环境配置，无法全部删除")

        cfg["environments"] = [e for e in cfg["environments"] if e.get("id") != env_id]

        if cfg["active_env_id"] == env_id:
            cfg["active_env_id"] = cfg["environments"][0]["id"]

        _write_config_unlocked(cfg)
        return get_environments_payload(mask=True)
