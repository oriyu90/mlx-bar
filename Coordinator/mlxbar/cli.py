from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .settings import app_support_dir


EXIT_CODES = {"PORT_IN_USE": 4, "MODEL_INCOMPATIBLE": 5, "UPDATE_PROBE_FAILED": 6,
              "AUTHENTICATION_FAILED": 7}


class Client:
    def __init__(self):
        self.socket = app_support_dir() / "control" / "coordinator.sock"

    def request(self, method: str, path: str, body: dict | None = None):
        if not self.socket.exists():
            raise ConnectionError("MLXBarサービスが起動していません")
        transport = httpx.HTTPTransport(uds=str(self.socket))
        with httpx.Client(transport=transport, base_url="http://mlxbar", timeout=None) as client:
            response = client.request(method, path, json=body)
            if response.status_code >= 400:
                detail = response.json().get("detail", {})
                error = RuntimeError(detail.get("message") or detail.get("code") or response.text)
                error.code = detail.get("code", "INTERNAL_ERROR")
                raise error
            return response


def wait_job(client: Client, job: dict, as_json: bool) -> dict:
    while job.get("state") not in {"completed", "failed", "cancelled"}:
        if not as_json:
            print(f"{job.get('state')}: {job.get('message', '')}", file=sys.stderr)
        time.sleep(0.5)
        job = client.request("GET", f"/api/v1/jobs/{job['id']}").json()
    return job


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="mlxbarctl")
    root.add_argument("--json", action="store_true", dest="global_json")
    root.add_argument("--start", action="store_true")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    model = sub.add_parser("model").add_subparsers(dest="action", required=True)
    model.add_parser("list")
    scan = model.add_parser("scan"); scan.add_argument("--wait", action="store_true")
    probe = model.add_parser("probe"); probe.add_argument("model_id")
    load = model.add_parser("load"); load.add_argument("model_id"); load.add_argument("--engine", default="auto")
    model.add_parser("unload")
    add_folder = model.add_parser("add-folder"); add_folder.add_argument("path")
    remove_folder = model.add_parser("remove-folder"); remove_folder.add_argument("path")
    gen = sub.add_parser("generate"); gen.add_argument("--prompt", required=True); gen.add_argument("--image", action="append", default=[])
    gen.add_argument("--temperature", type=float); gen.add_argument("--top-p", type=float)
    gen.add_argument("--repetition-penalty", type=float); gen.add_argument("--repetition-context-size", type=int)
    gen.add_argument("--max-tokens", type=int, default=512)
    gen.add_argument("--request-id"); gen.add_argument("--stream-events", action="store_true")
    cancel = sub.add_parser("cancel"); cancel.add_argument("request_id")
    sub.add_parser("cancel-all")
    runtime = sub.add_parser("runtime").add_subparsers(dest="action", required=True)
    runtime.add_parser("list")
    check = runtime.add_parser("check"); check.add_argument("engine")
    stage = runtime.add_parser("stage"); stage.add_argument("engine"); stage.add_argument("--version"); stage.add_argument("--git-ref"); stage.add_argument("--wait", action="store_true")
    activate = runtime.add_parser("activate"); activate.add_argument("engine"); activate.add_argument("slot_id")
    rollback = runtime.add_parser("rollback"); rollback.add_argument("engine")
    update = runtime.add_parser("update"); update.add_argument("engine"); update.add_argument("--wait", action="store_true")
    delete_slot = runtime.add_parser("delete-slot"); delete_slot.add_argument("engine"); delete_slot.add_argument("slot_id")
    cancel_job = runtime.add_parser("cancel-job"); cancel_job.add_argument("engine")
    config = sub.add_parser("config").add_subparsers(dest="action", required=True)
    config.add_parser("get")
    set_cmd = config.add_parser("set"); set_cmd.add_argument("key"); set_cmd.add_argument("value")
    set_language = config.add_parser("set-language"); set_language.add_argument("language", choices=["en", "ja"])
    set_max_tokens = config.add_parser("set-max-tokens"); set_max_tokens.add_argument("value", type=int)
    set_queue = config.add_parser("set-queue-limits")
    set_queue.add_argument("--max-queued", type=int, required=True)
    set_queue.add_argument("--timeout-seconds", type=int, required=True)
    set_sampling = config.add_parser("set-sampling-defaults")
    set_sampling.add_argument("--temperature", type=float, required=True)
    set_sampling.add_argument("--top-p", type=float, required=True)
    set_sampling.add_argument("--repetition-penalty", type=float, required=True)
    set_sampling.add_argument("--repetition-context-size", type=int, required=True)
    set_login = config.add_parser("set-launch-at-login")
    set_login.add_argument("value", choices=["true", "false"])
    secrets_group = sub.add_parser("secrets").add_subparsers(dest="action", required=True)
    secrets_group.add_parser("get-api-token")
    set_api_token = secrets_group.add_parser("set-api-token"); set_api_token.add_argument("token")
    secrets_group.add_parser("regenerate-api-token")
    secrets_group.add_parser("get-lmstudio-token")
    set_lm_token = secrets_group.add_parser("set-lmstudio-token"); set_lm_token.add_argument("token", nargs="?", default="")
    logs_group = sub.add_parser("logs").add_subparsers(dest="action", required=True)
    logs_show = logs_group.add_parser("show"); logs_show.add_argument("--limit", type=int, default=500)
    logs_group.add_parser("clear")
    network = sub.add_parser("network").add_subparsers(dest="action", required=True)
    lan = network.add_parser("set-lan")
    lan_group = lan.add_mutually_exclusive_group(required=True)
    lan_group.add_argument("--enabled", action="store_true")
    lan_group.add_argument("--disabled", action="store_true")
    set_port = network.add_parser("set-port"); set_port.add_argument("port", type=int)
    api = sub.add_parser("api").add_subparsers(dest="action", required=True)
    test = api.add_parser("test-port"); test.add_argument("port", type=int)
    sub.add_parser("diagnostics")
    remove_all = sub.add_parser("remove-all-data")
    remove_all.add_argument("--yes", action="store_true")
    return root


def nested_patch(key: str, value: str) -> dict:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    result = current = {}
    parts = key.split(".")
    for part in parts[:-1]:
        current[part] = {}
        current = current[part]
    current[parts[-1]] = parsed
    return result


def execute(args, client: Client):
    if args.command == "status": return client.request("GET", "/api/v1/status").json()
    if args.command == "model":
        if args.action == "list": return client.request("GET", "/api/v1/models").json()
        if args.action == "scan":
            job = client.request("POST", "/api/v1/models/scan").json()
            return wait_job(client, job, args.global_json) if args.wait else job
        if args.action == "probe": return client.request("POST", f"/api/v1/models/{args.model_id}/probe").json()
        if args.action == "load": return client.request("POST", f"/api/v1/models/{args.model_id}/load", {"engine": args.engine}).json()
        if args.action == "unload": return client.request("DELETE", "/api/v1/models/loaded").json()
        if args.action == "add-folder":
            roots = client.request("GET", "/api/v1/settings").json().get("models", {}).get("roots", [])
            if args.path not in roots: roots.append(args.path)
            client.request("PUT", "/api/v1/settings", {"models": {"roots": roots}})
            job = client.request("POST", "/api/v1/models/scan").json()
            return wait_job(client, job, args.global_json)
        if args.action == "remove-folder":
            roots = [path for path in client.request("GET", "/api/v1/settings").json().get("models", {}).get("roots", [])
                     if path != args.path]
            client.request("PUT", "/api/v1/settings", {"models": {"roots": roots}})
            job = client.request("POST", "/api/v1/models/scan").json()
            return wait_job(client, job, args.global_json)
    if args.command == "generate":
        payload = generation_payload(args)
        response = client.request("POST", "/api/v1/generate", payload)
        text = ""
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if event.get("type") == "delta": text += event["text"]
                elif event.get("type") == "error":
                    error = RuntimeError(event.get("message") or event.get("code") or "生成に失敗しました")
                    error.code = event.get("code", "INTERNAL_ERROR")
                    raise error
        return {"text": text}
    if args.command == "cancel":
        return client.request("POST", f"/api/v1/generate/{args.request_id}/cancel").json()
    if args.command == "cancel-all":
        return client.request("POST", "/api/v1/generate/cancel-all").json()
    if args.command == "runtime":
        if args.action == "list": return client.request("GET", "/api/v1/runtimes").json()
        if args.action == "check": return client.request("POST", f"/api/v1/runtimes/{args.engine}/check").json()
        if args.action == "stage":
            job = client.request("POST", f"/api/v1/runtimes/{args.engine}/stage", {"version": args.version, "gitRef": args.git_ref}).json()
            return wait_job(client, job, args.global_json) if args.wait else job
        if args.action == "activate": return client.request("POST", f"/api/v1/runtimes/{args.engine}/activate", {"slotId": args.slot_id}).json()
        if args.action == "rollback": return client.request("POST", f"/api/v1/runtimes/{args.engine}/rollback").json()
        if args.action == "update":
            job = client.request("POST", f"/api/v1/runtimes/{args.engine}/update").json()
            return wait_job(client, job, args.global_json) if args.wait else job
        if args.action == "delete-slot":
            return client.request("DELETE", f"/api/v1/runtimes/{args.engine}/slots/{args.slot_id}").json()
        if args.action == "cancel-job":
            runtimes = client.request("GET", "/api/v1/runtimes").json()
            job = (runtimes.get(args.engine) or {}).get("activeJob")
            if not job:
                return {"cancelled": False, "message": "実行中のジョブはありません"}
            return client.request("POST", f"/api/v1/runtimes/{args.engine}/jobs/{job['id']}/cancel").json()
    if args.command == "config":
        if args.action == "get": return client.request("GET", "/api/v1/settings").json()
        if args.action == "set-language":
            return client.request("PUT", "/api/v1/settings", {"general": {"language": args.language}}).json()
        if args.action == "set-max-tokens":
            if not 1 <= args.value <= 2_000_000:
                raise ValueError("Max token上限は1〜2,000,000で指定してください")
            return client.request("PUT", "/api/v1/settings", {"generation": {"maxTokens": args.value}}).json()
        if args.action == "set-queue-limits":
            if not 1 <= args.max_queued <= 64:
                raise ValueError("生成待ち件数は1〜64で指定してください")
            if not 10 <= args.timeout_seconds <= 7200:
                raise ValueError("最大待ち時間は10〜7,200秒で指定してください")
            return client.request("PUT", "/api/v1/settings", {"generation": {
                "maxQueuedRequests": args.max_queued, "queueTimeoutSeconds": args.timeout_seconds}}).json()
        if args.action == "set-sampling-defaults":
            if not 0 <= args.temperature <= 2: raise ValueError("温度は0〜2で指定してください")
            if not 0 <= args.top_p <= 1: raise ValueError("Top Pは0〜1で指定してください")
            if not 0.01 <= args.repetition_penalty <= 2: raise ValueError("繰り返しペナルティは0.01〜2で指定してください")
            if not 1 <= args.repetition_context_size <= 32768: raise ValueError("ペナルティ対象範囲は1〜32,768 tokensで指定してください")
            return client.request("PUT", "/api/v1/settings", {"generation": {
                "defaultTemperature": args.temperature, "defaultTopP": args.top_p,
                "defaultRepetitionPenalty": args.repetition_penalty,
                "repetitionContextSize": args.repetition_context_size}}).json()
        if args.action == "set-launch-at-login":
            # Only changes the *desired* state. SMAppService (the actual macOS
            # login-item registration) is a Swift/ObjC-only API with no CLI
            # equivalent -- the running GUI app reconciles this setting
            # against the real registration on its next launch/refresh.
            return client.request("PUT", "/api/v1/settings",
                                  {"general": {"launchAtLogin": args.value == "true"}}).json()
        return client.request("PUT", "/api/v1/settings", nested_patch(args.key, args.value)).json()
    if args.command == "secrets":
        if args.action == "get-api-token": return client.request("GET", "/api/v1/settings/api-token").json()
        if args.action == "set-api-token": return client.request("PUT", "/api/v1/settings/api-token", {"token": args.token}).json()
        if args.action == "regenerate-api-token": return client.request("POST", "/api/v1/settings/api-token/regenerate").json()
        if args.action == "get-lmstudio-token": return client.request("GET", "/api/v1/settings/lm-studio-token").json()
        if args.action == "set-lmstudio-token": return client.request("PUT", "/api/v1/settings/lm-studio-token", {"token": args.token}).json()
    if args.command == "logs":
        if args.action == "show": return client.request("GET", f"/api/v1/logs?limit={args.limit}").json()
        if args.action == "clear": return client.request("DELETE", "/api/v1/logs").json()
    if args.command == "network":
        if args.action == "set-lan":
            # Must be a single atomic PUT: SettingsStore._validate requires
            # api.host and security.allowLan to change together (see
            # settings.py), so two separate `config set` calls can't express
            # this -- each one alone would fail validation.
            enabled = args.enabled
            return client.request("PUT", "/api/v1/settings", {
                "api": {"host": "0.0.0.0" if enabled else "127.0.0.1", "requireToken": True},
                "security": {"allowLan": enabled},
            }).json()
        if args.action == "set-port":
            result = client.request("POST", "/api/v1/settings/api-listener/test", {"port": args.port}).json()
            if not result.get("available"):
                return result
            return client.request("PUT", "/api/v1/settings", {"api": {"port": args.port}}).json()
    if args.command == "api": return client.request("POST", "/api/v1/settings/api-listener/test", {"port": args.port}).json()
    if args.command == "diagnostics": return client.request("GET", "/api/v1/diagnostics").json()
    if args.command == "remove-all-data": return remove_all_data(args, client)


SERVICE_LABEL = "com.yukiorita.MLXBar.Coordinator"


def remove_all_data(args, client: Client) -> dict:
    """Wipes all MLXBar data and stops the background service.

    Calls the same `POST /api/v1/system/reset` endpoint the GUI's "Remove
    all data and quit" uses (see Coordinator/mlxbar/state.py's
    `reset_all()`), which owns wiping the coordinator's own data directory,
    then does its own best-effort OS-registration cleanup here in Python.

    Known limitation: this cannot call `SMAppService.unregister()` --
    that's a Swift/ObjC-only API with no CLI/shell equivalent. `launchctl
    bootout` (below) already fully stops and de-schedules the background
    service, so this does not block a clean reinstall; the only visible
    effect is a stale entry under System Settings > Login Items until
    MLXBar.app itself is deleted from disk.
    """
    if not args.yes:
        raise RuntimeError(
            "この操作は設定・APIキー・モデルデータベース・ダウンロード済みランタイム・ログを削除し、"
            "サービスを停止します。実行するには mlxbarctl remove-all-data --yes を指定してください。"
            "外部のモデル本体（Hugging Faceキャッシュ等）は削除されません。"
        )
    with contextlib.suppress(Exception):
        client.request("POST", "/api/v1/system/reset")

    for _ in range(20):
        if not client.socket.exists():
            break
        try:
            client.request("GET", "/api/v1/health")
        except Exception:
            break
        time.sleep(0.1)

    domain = f"gui/{os.getuid()}"
    fallback_plist = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    subprocess.run(["launchctl", "bootout", f"{domain}/{SERVICE_LABEL}"],
                   check=False, capture_output=True, timeout=10)
    subprocess.run(["launchctl", "bootout", domain, str(fallback_plist)],
                   check=False, capture_output=True, timeout=10)
    subprocess.run(["defaults", "delete", "com.yukiorita.MLXBar"],
                   check=False, capture_output=True, timeout=10)

    home = Path.home()
    paths = [
        home / "Library" / "Logs" / "MLXBar",
        home / "Library" / "Caches" / "com.yukiorita.MLXBar",
        home / "Library" / "Saved Application State" / "com.yukiorita.MLXBar.savedState",
        home / "Library" / "Preferences" / "com.yukiorita.MLXBar.plist",
        fallback_plist,
        Path("/tmp/mlxbar-coordinator.log"),
        Path("/tmp/mlxbar-dev.log"),
    ]
    removal_errors = []
    for path in paths:
        if not path.exists():
            continue
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        except OSError as exc:
            removal_errors.append(f"{path.name}: {exc}")

    return {
        "removed": True,
        "errors": removal_errors,
        "note": "アプリ本体を削除するまで、システム設定 > ログイン項目に見た目上のエントリが残る場合があります"
                "（launchctl bootout済みのため、再インストール時の動作には影響しません）。",
    }


def main() -> None:
    argv = sys.argv[1:]
    anywhere_json = "--json" in argv
    argv = [item for item in argv if item != "--json"]
    args = parser().parse_args(argv)
    args.global_json = anywhere_json
    try:
        if args.command == "generate" and args.stream_events:
            stream_events(args, Client())
            return
        result = execute(args, Client())
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.global_json else human(result))
    except ConnectionError as exc:
        if args.start:
            subprocess.run(["launchctl", "kickstart", "-k",
                            f"gui/{os.getuid()}/com.yukiorita.MLXBar.Coordinator"], check=False)
            time.sleep(1)
            try:
                result = execute(args, Client())
                print(json.dumps(result, ensure_ascii=False, indent=2) if args.global_json else human(result))
                return
            except Exception:
                pass
        print(str(exc), file=sys.stderr); raise SystemExit(3)
    except Exception as exc:
        print(str(exc), file=sys.stderr); raise SystemExit(EXIT_CODES.get(getattr(exc, "code", ""), 8))


def human(value) -> str:
    if isinstance(value, dict) and set(value) == {"text"}:
        return value["text"]
    return json.dumps(value, ensure_ascii=False, indent=2)


def stream_events(args, client: Client) -> None:
    if not client.socket.exists():
        raise ConnectionError("MLXBarサービスが起動していません")
    transport = httpx.HTTPTransport(uds=str(client.socket))
    payload = generation_payload(args)
    with httpx.Client(transport=transport, base_url="http://mlxbar", timeout=None) as http:
        with http.stream("POST", "/api/v1/generate", json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    print(line[6:], flush=True)
                    if event.get("type") == "error":
                        error = RuntimeError(event.get("message") or event.get("code") or "生成に失敗しました")
                        error.code = event.get("code", "INTERNAL_ERROR")
                        raise error


def generation_payload(args) -> dict:
    payload = {"prompt": args.prompt, "images": args.image,
               "max_tokens": args.max_tokens, "requestId": args.request_id}
    for argument, key in (("temperature", "temperature"), ("top_p", "top_p"),
                          ("repetition_penalty", "repetition_penalty"),
                          ("repetition_context_size", "repetition_context_size")):
        value = getattr(args, argument, None)
        if value is not None:
            payload[key] = value
    return payload


if __name__ == "__main__":
    main()
