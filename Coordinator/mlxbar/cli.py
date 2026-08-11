from __future__ import annotations

import argparse
import json
import os
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
    gen = sub.add_parser("generate"); gen.add_argument("--prompt", required=True); gen.add_argument("--image", action="append", default=[])
    gen.add_argument("--temperature", type=float); gen.add_argument("--top-p", type=float)
    gen.add_argument("--repetition-penalty", type=float); gen.add_argument("--repetition-context-size", type=int)
    gen.add_argument("--max-tokens", type=int, default=512)
    gen.add_argument("--request-id"); gen.add_argument("--stream-events", action="store_true")
    cancel = sub.add_parser("cancel"); cancel.add_argument("request_id")
    runtime = sub.add_parser("runtime").add_subparsers(dest="action", required=True)
    runtime.add_parser("list")
    check = runtime.add_parser("check"); check.add_argument("engine")
    stage = runtime.add_parser("stage"); stage.add_argument("engine"); stage.add_argument("--version"); stage.add_argument("--git-ref"); stage.add_argument("--wait", action="store_true")
    activate = runtime.add_parser("activate"); activate.add_argument("engine"); activate.add_argument("slot_id")
    rollback = runtime.add_parser("rollback"); rollback.add_argument("engine")
    update = runtime.add_parser("update"); update.add_argument("engine"); update.add_argument("--wait", action="store_true")
    config = sub.add_parser("config").add_subparsers(dest="action", required=True)
    config.add_parser("get")
    set_cmd = config.add_parser("set"); set_cmd.add_argument("key"); set_cmd.add_argument("value")
    api = sub.add_parser("api").add_subparsers(dest="action", required=True)
    test = api.add_parser("test-port"); test.add_argument("port", type=int)
    sub.add_parser("diagnostics")
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
    if args.command == "config":
        if args.action == "get": return client.request("GET", "/api/v1/settings").json()
        return client.request("PUT", "/api/v1/settings", nested_patch(args.key, args.value)).json()
    if args.command == "api": return client.request("POST", "/api/v1/settings/api-listener/test", {"port": args.port}).json()
    if args.command == "diagnostics": return client.request("GET", "/api/v1/diagnostics").json()


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
