#!/usr/bin/env python3
"""Account pool: route order, cooldowns, session bindings and rotation choice.

Vendored from the Omnigent payload module so it runs standalone. The routing,
cooldown and locking logic is deliberately unchanged -- it is the part of the
system with the most history behind it. What was removed is the Omnigent bridge
protocol: the runtime and rotation-request files that coordinated with a patched
Omnigent process. The native supervisor replaces those.

State lives where it always did, so an existing installation keeps its accounts,
cooldowns and bindings.
"""

from __future__ import annotations
import asyncio
import contextlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

CONFIG_PATH = Path("~/.omnigent/codex-account-pool.json").expanduser()
STATE_PATH = Path("~/.omnigent/codex-account-pool-state.json").expanduser()
ACCOUNTS_DIR = Path("~/.omnigent/codex-accounts").expanduser()
RUNTIME_FILE = "codex-account-runtime.json"
ROTATE_FILE = "codex-account-rotate.json"
DEFAULT_ROTATE_AT_PERCENT = 90.0
# The dashboard may not exceed this, and a stored value above it is clamped
# rather than rejected, so an existing configuration keeps working.
MAX_ROTATE_AT_PERCENT = 95.0
# Preparation always begins this many percentage points before the switch.
PREPARATION_OFFSET = 3.0
DEFAULT_UNKNOWN_COOLDOWN_SECONDS = 6 * 60 * 60
ROTATION_WAIT_SECONDS = 60.0


class AccountPoolError(RuntimeError):
    """Invalid account-pool configuration or state."""


@dataclass(frozen=True)
class AccountProfile:
    name: str
    auth_json: Path | None = None
    provider: str = "codex"
    config_dir: Path | None = None
    use_default_config: bool = False
    # Where the desktop app keeps this account's profile. Defaults to
    # ~/.omnigent/claude-desktop/<name>, but an account already signed into the
    # app's default profile can point at it instead of being signed in twice.
    desktop_user_data_dir: Path | None = None


def clamp_threshold(value: float) -> float:
    """Hold a switch threshold inside the allowed range.

    Clamping rather than rejecting matters for migration: a configuration
    written before the cap existed must keep working instead of refusing to
    load.
    """
    return max(1.0, min(float(value), MAX_ROTATE_AT_PERCENT))


def preparation_percent(rotate_at_percent: float) -> float:
    """Where preparation begins: always the offset below the switch."""
    return max(0.0, clamp_threshold(rotate_at_percent) - PREPARATION_OFFSET)


@dataclass(frozen=True)
class PoolConfig:
    accounts: tuple[AccountProfile, ...]
    rotate_at_percent: float = DEFAULT_ROTATE_AT_PERCENT
    claude_fallback_agent: str | None = None
    enabled: bool = True

    @property
    def preparation_at_percent(self) -> float:
        return preparation_percent(self.rotate_at_percent)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "PoolConfig":
        if not path.exists():
            return cls(accounts=(), enabled=False)
        raw = _read_json(path)
        if not isinstance(raw, dict):
            raise AccountPoolError(f"{path} must contain a JSON object")
        values = raw.get("accounts", [])
        if not isinstance(values, list):
            raise AccountPoolError("'accounts' must be an array")
        accounts: list[AccountProfile] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            name = value.get("name")
            provider = value.get("provider", "codex")
            if not isinstance(name, str) or not name.strip():
                raise AccountPoolError("each account needs a non-empty name")
            if provider not in {"codex", "claude"}:
                raise AccountPoolError("unknown subscription provider")
            key = "auth_json" if provider == "codex" else "config_dir"
            location = value.get(key)
            if not isinstance(location, str) or not location.strip():
                raise AccountPoolError(f"account needs {key}")
            accounts.append(AccountProfile(
                name.strip(),
                Path(location).expanduser() if provider == "codex" else None,
                provider,
                Path(location).expanduser() if provider == "claude" else None,
                value.get("use_default_config") is True,
                Path(value["desktop_user_data_dir"]).expanduser()
                if isinstance(value.get("desktop_user_data_dir"), str)
                else None,
            ))
        if raw.get("claude_fallback_agent") and not any(a.provider == "claude" for a in accounts):
            legacy_name = "claude-legacy"
            number = 1
            while any(a.name == legacy_name for a in accounts):
                legacy_name = f"claude-legacy-{number}"
                number += 1
            accounts.append(AccountProfile(
                legacy_name, provider="claude",
                config_dir=Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser(),
                use_default_config=not bool(os.environ.get("CLAUDE_CONFIG_DIR")),
            ))
        if len({a.name for a in accounts}) != len(accounts):
            raise AccountPoolError("account names must be unique")
        threshold = raw.get("rotate_at_percent", DEFAULT_ROTATE_AT_PERCENT)
        if not isinstance(threshold, (int, float)) or not 0 < float(threshold) <= 100:
            raise AccountPoolError("rotate_at_percent must be > 0 and <= 100")
        threshold = clamp_threshold(float(threshold))
        fallback_raw = raw.get("claude_fallback_agent")
        fallback = fallback_raw.strip() if isinstance(fallback_raw, str) and fallback_raw.strip() else None
        return cls(
            accounts=tuple(accounts),
            rotate_at_percent=float(threshold),
            claude_fallback_agent=fallback,
            enabled=bool(raw.get("enabled", True)) and bool(accounts),
        )


@dataclass(frozen=True)
class RateLimitDecision:
    rotate: bool
    used_percent: float | None
    retry_at: int | None
    reason: str


class CodexRequestClient(Protocol):
    async def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, Any]: ...


class AccountPool:
    def __init__(self, config: PoolConfig, *, state_path: Path = STATE_PATH, now: Any = time.time) -> None:
        self.config = config
        self.state_path = state_path.expanduser()
        self._now = now

    @classmethod
    def from_default(cls) -> "AccountPool":
        return cls(PoolConfig.load())

    @property
    def enabled(self) -> bool:
        return self.config.enabled and bool(self.config.accounts)

    def validate(self) -> list[str]:
        if not self.enabled:
            return ["pool is disabled or contains no accounts"]
        problems: list[str] = []
        for account in self.config.accounts:
            if not account_has_credential(account):
                problems.append(f"{account.name}: missing/unusable {account.auth_json}")
        return problems

    def account_for_session(
        self,
        session_id: str,
        *,
        provider: str | None = None,
    ) -> AccountProfile | None:
        if not self.enabled:
            return None
        now = int(self._now())
        with self._locked_state() as state:
            self._prune(state, now)
            bindings = state.setdefault("session_bindings", {})
            bound = bindings.get(session_id)
            if isinstance(bound, str):
                profile = self._by_name(bound)
                if (
                    profile is not None
                    and (provider is None or profile.provider == provider)
                    and account_has_credential(profile)
                    and self._available(state, bound, now)
                ):
                    return profile
            account = self._choose(state, now, provider=provider)
            if account is None:
                bindings.pop(session_id, None)
                return None
            bindings[session_id] = account.name
            state["current_account"] = account.name
            return account

    def rotate_session(
        self,
        session_id: str,
        *,
        exhausted_account: str | None,
        retry_at: int | None,
        reason: str,
        provider: str | None = None,
        fallback_to_other_providers: bool = False,
    ) -> AccountProfile | None:
        if not self.enabled:
            return None
        now = int(self._now())
        selected_provider = provider
        if selected_provider is None and exhausted_account:
            exhausted_profile = self._by_name(exhausted_account)
            selected_provider = exhausted_profile.provider if exhausted_profile else None
        with self._locked_state() as state:
            self._prune(state, now)
            if exhausted_account:
                state.setdefault("cooldowns", {})[exhausted_account] = {
                    "retry_at": int(retry_at) if retry_at else now + DEFAULT_UNKNOWN_COOLDOWN_SECONDS,
                    "reason": reason,
                    "marked_at": now,
                }
            account = self._choose(
                state,
                now,
                exclude={exhausted_account} if exhausted_account else set(),
                provider=selected_provider,
            )
            if account is None and fallback_to_other_providers:
                account = self._choose(
                    state,
                    now,
                    exclude={exhausted_account} if exhausted_account else set(),
                )
            bindings = state.setdefault("session_bindings", {})
            if account is None:
                bindings.pop(session_id, None)
                return None
            bindings[session_id] = account.name
            state["current_account"] = account.name
            return account

    def snapshot(self) -> dict[str, Any]:
        with self._locked_state() as state:
            return json.loads(json.dumps(state))

    def _by_name(self, name: str) -> AccountProfile | None:
        return next((a for a in self.config.accounts if a.name == name), None)

    def _choose(
        self,
        state: dict[str, Any],
        now: int,
        *,
        exclude: set[str] | None = None,
        provider: str | None = None,
    ) -> AccountProfile | None:
        """Choose the first available account in configured route order."""
        exclude = exclude or set()
        for profile in self.config.accounts:
            if profile.name in exclude:
                continue
            if provider is not None and profile.provider != provider:
                continue
            if account_has_credential(profile) and self._available(state, profile.name, now):
                return profile
        return None

    @staticmethod
    def _available(state: dict[str, Any], name: str, now: int) -> bool:
        value = state.setdefault("cooldowns", {}).get(name)
        if not isinstance(value, dict):
            return True
        retry_at = value.get("retry_at")
        return not isinstance(retry_at, (int, float)) or int(retry_at) <= now

    @staticmethod
    def _prune(state: dict[str, Any], now: int) -> None:
        cooldowns = state.setdefault("cooldowns", {})
        expired = [name for name, value in cooldowns.items() if isinstance(value, dict) and isinstance(value.get("retry_at"), (int, float)) and int(value["retry_at"]) <= now]
        for name in expired:
            cooldowns.pop(name, None)

    @contextlib.contextmanager
    def _locked_state(self) -> Iterator[dict[str, Any]]:
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                state = _read_json(self.state_path) if self.state_path.exists() else {}
                if not isinstance(state, dict):
                    state = {}
                state.setdefault("version", 1)
                state.setdefault("cooldowns", {})
                state.setdefault("session_bindings", {})
                yield state
                _atomic_json(self.state_path, state)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def auth_json_has_credential(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        raw = _read_json(path.expanduser())
    except Exception:
        return False
    if not isinstance(raw, dict):
        return False
    tokens = raw.get("tokens")
    if isinstance(tokens, dict) and any(isinstance(tokens.get(k), str) and bool(tokens.get(k).strip()) for k in ("access_token", "refresh_token", "id_token")):
        return True
    return any(isinstance(raw.get(k), str) and bool(raw.get(k).strip()) for k in ("OPENAI_API_KEY", "personal_access_token"))


_CLAUDE_AUTH_CACHE: dict[tuple[str, bool], tuple[float, bool]] = {}


def claude_account_env(profile: AccountProfile) -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE",
        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CONFIG_DIR",
    ):
        env.pop(key, None)
    if not profile.use_default_config and profile.config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(profile.config_dir)
    return env


def account_has_credential(profile: AccountProfile) -> bool:
    if profile.provider == "codex":
        return auth_json_has_credential(profile.auth_json)
    if profile.config_dir is None:
        return False
    key = (str(profile.config_dir), profile.use_default_config)
    cached = _CLAUDE_AUTH_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 30:
        return cached[1]
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"], env=claude_account_env(profile),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, check=False,
        )
        status = json.loads(result.stdout)
        valid = result.returncode == 0 and isinstance(status, dict) and status.get("loggedIn") is True and status.get("authMethod") == "claude.ai"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        valid = False
    _CLAUDE_AUTH_CACHE[key] = (time.monotonic(), valid)
    return valid


def bind_account_auth(private_codex_home: Path, source_auth_json: Path) -> None:
    """Link only auth.json; all other private-home state remains Omnigent-owned."""
    source = source_auth_json.expanduser().resolve()
    if not auth_json_has_credential(source):
        raise AccountPoolError(f"unusable Codex auth file: {source}")
    private_codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = private_codex_home / "auth.json"
    with contextlib.suppress(FileNotFoundError):
        target.unlink()
    target.symlink_to(source)


def decide_rate_limits(payload: Mapping[str, Any], *, rotate_at_percent: float) -> RateLimitDecision:
    """Interpret both current and older Codex app-server quota payloads."""
    value: Any = payload
    if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping):
        value = value["result"]
    if not isinstance(value, Mapping):
        return RateLimitDecision(False, None, None, "no rate-limit data")

    allowed = value.get("ordinaryUsageAllowed", value.get("ordinary_usage_allowed"))
    snapshot: Any = value.get("rateLimits", value.get("rate_limits", value))
    if not isinstance(snapshot, Mapping):
        snapshot = value
    if allowed is None:
        allowed = snapshot.get("ordinaryUsageAllowed", snapshot.get("ordinary_usage_allowed"))
    spend_reached = snapshot.get("spendControlReached", snapshot.get("spend_control_reached"))
    reached_type = snapshot.get("rateLimitReachedType", snapshot.get("rate_limit_reached_type"))

    max_used: float | None = None
    threshold_resets: list[int] = []
    all_resets: list[int] = []
    threshold_hit = False
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, Mapping):
            continue
        used = window.get("usedPercent", window.get("used_percent"))
        try:
            used_float = float(used) if used is not None else None
        except (TypeError, ValueError):
            used_float = None
        if used_float is not None:
            max_used = used_float if max_used is None else max(max_used, used_float)
            threshold_hit = threshold_hit or used_float >= rotate_at_percent
        reset = window.get("resetsAt", window.get("resets_at"))
        try:
            reset_int = int(reset) if reset is not None else None
        except (TypeError, ValueError):
            reset_int = None
        if reset_int:
            all_resets.append(reset_int)
            if used_float is not None and used_float >= rotate_at_percent:
                threshold_resets.append(reset_int)

    if allowed is False:
        return RateLimitDecision(True, max_used, max(all_resets, default=None), "Codex reports ordinary usage unavailable")
    if bool(spend_reached):
        return RateLimitDecision(True, max_used, max(all_resets, default=None), "Codex spend control reached")
    if isinstance(reached_type, str) and reached_type.strip():
        return RateLimitDecision(True, max_used, max(all_resets, default=None), f"Codex rate limit reached ({reached_type})")
    if threshold_hit:
        return RateLimitDecision(True, max_used, max(threshold_resets, default=None), f"Codex usage reached {rotate_at_percent:g}% rotation threshold")
    return RateLimitDecision(False, max_used, None, "Codex account available")


async def preflight_rotation_request(client: CodexRequestClient, bridge_dir: Path, *, session_id: str) -> tuple[bool, int]:
    """Ask the runner to rotate before a new turn if quota is effectively spent."""
    config = PoolConfig.load()
    if not config.enabled:
        return False, runtime_generation(bridge_dir)
    runtime = read_runtime(bridge_dir)
    if not runtime or runtime.get("mode") != "codex":
        return False, runtime_generation(bridge_dir)
    account_name = runtime.get("account_name")
    if not isinstance(account_name, str):
        return False, runtime_generation(bridge_dir)
    generation = int(runtime.get("generation", 0))
    try:
        response = await client.request("account/rateLimits/read", {})
        decision = decide_rate_limits(response, rotate_at_percent=config.rotate_at_percent)
    except Exception:
        return False, generation
    if not decision.rotate:
        return False, generation
    request_rotation(bridge_dir, session_id=session_id, account_name=account_name, retry_at=decision.retry_at, reason=decision.reason, replay_required=False)
    return True, generation


async def wait_for_rotation(bridge_dir: Path, *, after_generation: int, timeout: float = ROTATION_WAIT_SECONDS) -> dict[str, Any] | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        runtime = read_runtime(bridge_dir)
        if runtime is not None and int(runtime.get("generation", 0)) > after_generation:
            return runtime
        await asyncio.sleep(0.2)
    return None


def is_usage_limit_payload(value: object) -> bool:
    """Recognize Codex's structured quota error recursively."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in {"codexerrorinfo", "codex_error_info"} and _contains_usage_variant(item):
                return True
            if is_usage_limit_payload(item):
                return True
        return False
    if isinstance(value, list):
        return any(is_usage_limit_payload(item) for item in value)
    return False


def _contains_usage_variant(value: object) -> bool:
    if isinstance(value, str):
        compact = "".join(ch for ch in value.casefold() if ch.isalnum())
        return compact in {"usagelimitexceeded", "usagelimit", "quotaexceeded", "ratelimitexceeded"}
    if isinstance(value, Mapping):
        return any(_contains_usage_variant(v) for v in value.values())
    return False


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


# Backwards-compatible alias: the pool is no longer Codex-specific.
CodexAccountPool = AccountPool
