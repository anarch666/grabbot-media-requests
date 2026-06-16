#!/usr/bin/env python3
"""Log-triggered model failover for grabbot.

Cron/no-agent pattern:
- normal healthy run prints nothing
- quota/auth/rate-limit/model failures trigger fallback probing
- prints only when it switches model or cannot recover
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

HOME = Path.home()
PROFILE = HOME / ".hermes" / "profiles" / "media-requests"
CONFIG = PROFILE / "config.yaml"
STATE_DIR = PROFILE / "state"
STATE = STATE_DIR / "grabbot_model_failover.json"
LOCK = STATE_DIR / "grabbot_model_failover.lock"
SERVICE = "hermes-media-bot"
PROFILE_NAME = "media-requests"

CURRENT_HEALTH_PROMPT = (
    "Internal grabbot health check. Do not use tools. Any short response is fine."
)

# Ordered cheapest/most acceptable fallbacks. Current model is tested separately first.
CANDIDATES = [
    {"provider": "nous", "model": "nvidia/nemotron-3-ultra:free", "label": "Nous free Nemotron"},
    {"provider": "nous", "model": "stepfun/step-3.7-flash:free", "label": "Nous free Stepfun"},
    {"provider": "litellm-ct220", "model": "deepseek-v4-flash", "label": "LiteLLM DeepSeek flash"},
    {"provider": "litellm-ct220", "model": "mimo-v2.5", "label": "LiteLLM MiMo v2.5"},
    # Last resort. Tested as functional-ish but can be verbose/weird.
    {"provider": "litellm-ct220", "model": "ollama-143-gemma4-e2b", "label": "LiteLLM Ollama Gemma last resort"},
]

FAIL_PATTERNS = [
    r"api call failed",
    r"final error",
    r"rate.?limit",
    r"quota",
    r"insufficient",
    r"payment required",
    r"token.*exhaust",
    r"out of credits",
    r"billing",
    r"unauthori[sz]ed",
    r"authentication",
    r"invalid api key",
    r"401\b",
    r"402\b",
    r"429\b",
    r"no healthy deployments",
    r"notfounderror",
    r"model .*not found",
    r"model group=.*not found",
    r"provider.*failed",
    r"upstream.*error",
]
FAIL_RE = re.compile("|".join(f"(?:{p})" for p in FAIL_PATTERNS), re.I | re.S)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat()


def run(cmd: list[str], timeout: int = 60, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    full_env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if env:
        full_env.update(env)
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=full_env)


def load_state() -> dict[str, Any]:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(STATE)


def load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text()) or {}


def current_model(cfg: dict[str, Any]) -> tuple[str, str]:
    model_cfg = cfg.get("model") or {}
    return str(model_cfg.get("provider") or ""), str(model_cfg.get("default") or "")


def find_failure(text: str) -> str | None:
    match = FAIL_RE.search(text or "")
    if not match:
        return None
    reason = match.group(0).strip().replace("\n", " ")
    return reason[:120]


def scan_recent_logs(state: dict[str, Any]) -> tuple[bool, str]:
    # First run: only scan recent logs; don't dredge up ancient nonsense from humanity's long decline.
    since = state.get("last_checked_at") or "10 minutes ago"
    cmd = ["journalctl", "--user", "-u", SERVICE, "--since", since, "--no-pager"]
    try:
        cp = run(cmd, timeout=30)
    except Exception as exc:
        return True, f"journal scan failed: {type(exc).__name__}"
    combined = (cp.stdout or "") + "\n" + (cp.stderr or "")
    reason = find_failure(combined)
    return bool(reason), reason or ""


def service_active() -> bool:
    cp = run(["systemctl", "--user", "is-active", SERVICE], timeout=15)
    return cp.returncode == 0 and cp.stdout.strip() == "active"


def smoke(provider: str, model: str) -> tuple[bool, str]:
    cmd = [
        "timeout",
        "90",
        "hermes",
        "-p",
        PROFILE_NAME,
        "chat",
        "--provider",
        provider,
        "--model",
        model,
        "-q",
        CURRENT_HEALTH_PROMPT,
    ]
    try:
        cp = run(cmd, timeout=110)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    out = (cp.stdout or "") + "\n" + (cp.stderr or "")
    reason = find_failure(out)
    if cp.returncode != 0:
        return False, reason or f"exit {cp.returncode}"
    if reason:
        return False, reason
    return True, "ok"


def write_model(provider: str, model: str) -> Path:
    cfg = load_config()
    stamp = now_utc().strftime("%Y%m%d-%H%M%S")
    backup = CONFIG.with_name(f"config.yaml.bak-model-failover-{stamp}")
    backup.write_text(CONFIG.read_text())
    cfg.setdefault("model", {})["provider"] = provider
    cfg.setdefault("model", {})["default"] = model
    CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return backup


def restart_service() -> tuple[bool, str]:
    cp = run(["systemctl", "--user", "restart", SERVICE], timeout=60)
    if cp.returncode != 0:
        return False, ((cp.stderr or cp.stdout or "restart failed").strip()[:300])
    # Give systemd a second to be less theatrical.
    run(["sleep", "2"], timeout=5)
    return service_active(), "restarted"


def choose_fallback(current_provider: str, current_model_name: str) -> tuple[dict[str, str] | None, list[str]]:
    attempts: list[str] = []
    for cand in CANDIDATES:
        if cand["provider"] == current_provider and cand["model"] == current_model_name:
            continue
        ok, reason = smoke(cand["provider"], cand["model"])
        attempts.append(f"{cand['provider']} / {cand['model']}: {reason}")
        if ok:
            return cand, attempts
    return None, attempts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Force failover handling even if logs are clean")
    parser.add_argument("--dry-run", action="store_true", help="Probe but do not modify config or restart")
    parser.add_argument("--status", action="store_true", help="Print current watchdog/model state")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock_fp:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        state = load_state()
        cfg = load_config()
        provider, model = current_model(cfg)

        if args.status:
            print(f"Grabbot model: {provider} / {model}")
            print(f"Service active: {service_active()}")
            print(f"Last check: {state.get('last_checked_at', 'never')}")
            print(f"Last switch: {state.get('last_switch_at', 'never')}")
            return 0

        active = service_active()
        saw_failure, log_reason = scan_recent_logs(state)
        should_handle = args.force or saw_failure or not active

        if not should_handle:
            state["last_checked_at"] = iso_now()
            state["last_status"] = "healthy_no_failures_seen"
            save_state(state)
            return 0

        current_ok, current_reason = smoke(provider, model)
        trigger = "service inactive" if not active else (log_reason or current_reason or "forced")

        if current_ok and not args.force:
            if not active:
                restarted, restart_msg = restart_service()
                state["last_checked_at"] = iso_now()
                state["last_status"] = "service_restarted_model_healthy" if restarted else "service_restart_failed_model_healthy"
                state["last_reason"] = trigger
                save_state(state)
                print("Grabbot service was inactive; model is healthy.")
                print(f"Model unchanged: {provider} / {model}")
                print(f"Service: {'active after restart' if restarted else 'restart problem: ' + restart_msg}")
                return 0 if restarted else 1

            state["last_checked_at"] = iso_now()
            state["last_status"] = "failure_seen_but_current_model_healthy"
            state["last_reason"] = log_reason
            save_state(state)
            return 0

        chosen, attempts = choose_fallback(provider, model)
        if not chosen:
            state["last_checked_at"] = iso_now()
            state["last_status"] = "no_working_fallback"
            state["last_reason"] = trigger
            state["attempts"] = attempts
            save_state(state)
            print("Grabbot model failover could not find a working fallback.")
            print(f"Current: {provider} / {model} -> {current_reason}")
            print(f"Trigger: {trigger}")
            for line in attempts:
                print(f"- {line}")
            return 2

        if args.dry_run:
            print(f"DRY RUN: would switch grabbot from {provider} / {model} to {chosen['provider']} / {chosen['model']}")
            print(f"Trigger: {log_reason or current_reason or 'forced'}")
            return 0

        backup = write_model(chosen["provider"], chosen["model"])
        restarted, restart_msg = restart_service()

        state["last_checked_at"] = iso_now()
        state["last_switch_at"] = iso_now()
        state["last_status"] = "switched" if restarted else "switched_but_restart_failed"
        state["from"] = {"provider": provider, "model": model}
        state["to"] = {"provider": chosen["provider"], "model": chosen["model"]}
        state["last_reason"] = log_reason or current_reason or "forced"
        state["backup"] = str(backup)
        state["attempts"] = attempts
        save_state(state)

        print("Grabbot model failover switched provider/model.")
        print(f"From: {provider} / {model}")
        print(f"To: {chosen['provider']} / {chosen['model']} ({chosen['label']})")
        print(f"Trigger: {log_reason or current_reason or 'forced'}")
        print(f"Backup: {backup}")
        print(f"Service: {'active after restart' if restarted else 'restart problem: ' + restart_msg}")
        return 0 if restarted else 1


if __name__ == "__main__":
    raise SystemExit(main())
