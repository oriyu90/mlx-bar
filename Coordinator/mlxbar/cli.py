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
    unload = model.add_parser("unload")
    unload.add_argument("model_id", nargs="?", help="省略すると常駐モデルをすべて解放（従来動作）")
    unload.add_argument("--force", action="store_true")
    model.add_parser("resident")
    pin = model.add_parser("pin"); pin.add_argument("model_id")
    pin.add_argument("--max-memory-gb", type=float, default=None)
    pin.add_argument("--replicas", type=int, default=None,
                     help="同一モデルを並列生成用に複数常駐させる数（1〜8、要 models.pool.enabled）")
    unpin = model.add_parser("unpin"); unpin.add_argument("model_id")
    set_replicas = model.add_parser(
        "set-replicas", help="pin済みモデルの並列数（replicas）だけ変更（再ロードしない）")
    set_replicas.add_argument("model_id"); set_replicas.add_argument("count", type=int)
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
    set_pool = config.add_parser(
        "set-model-pool", help="複数モデル常駐の設定（指定したオプションだけ変更）")
    set_pool.add_argument("--enabled", choices=["true", "false"], default=None)
    set_pool.add_argument("--max-resident", type=int, default=None)
    set_pool.add_argument("--idle-ttl-seconds", type=int, default=None)
    set_pool.add_argument("--per-model-gb", type=int, default=None)
    set_pool.add_argument("--total-memory-percent", type=int, default=None)
    set_pool.add_argument("--system-reserve-gb", type=int, default=None)
    set_pool.add_argument("--generation-concurrency", type=int, default=None)
    set_pool.add_argument("--max-replicas-per-model", type=int, default=None)
    set_compression = config.add_parser(
        "set-context-compression",
        help="長い会話の古い部分を要約して短縮する設定（指定したオプションだけ変更）")
    set_compression.add_argument("--enabled", choices=["true", "false"], default=None)
    set_compression.add_argument("--trigger-percent", type=int, default=None,
                                 help="発火の目安（有効プロンプト上限に対する割合、50〜95）")
    set_compression.add_argument("--keep-tail", type=int, default=None,
                                 help="要約後も逐語で残す直近ターン数（2〜50）")
    set_compression.add_argument("--summary-max-tokens", type=int, default=None,
                                 help="要約生成の最大トークン数（100〜4000）")
    set_adaptive = config.add_parser(
        "set-adaptive-memory",
        help="実験的機能 アダプティブ・ハイブリッド・コンテキストメモリの設定（指定したオプションだけ変更、既定で無効）")
    set_adaptive.add_argument("--enabled", choices=["true", "false"], default=None)
    set_adaptive.add_argument("--policy", choices=["fidelity", "balanced", "memorySaver"], default=None)
    set_adaptive.add_argument("--trigger-percent", type=int, default=None, help="発火の目安（50〜95）")
    set_adaptive.add_argument("--memory-pressure-percent", type=int, default=None,
                              help="メモリ逼迫の目安（50〜95）")
    set_adaptive.add_argument("--keep-tail", type=int, default=None,
                              help="EXACTのまま残す直近ターン数（2〜50）")
    set_adaptive.add_argument("--max-latent-tokens", type=int, default=None,
                              help="LATENTブロックの最大ソフトトークン数（16〜2048）")
    set_adaptive.add_argument("--max-retrieved-segments", type=int, default=None,
                              help="取得するLATENTセグメントの上限（1〜64）")
    set_adaptive.add_argument("--verbatim-protection", choices=["true", "false"], default=None)
    set_adaptive.add_argument("--soft-token", choices=["true", "false"], default=None,
                              help="ソフトトークンのHybrid Prefill（実験、既定で無効）")
    set_flag = config.add_parser("set-flag", help="GUIのトグル設定を名前で切り替え")
    set_flag.add_argument("name", choices=["auto-load-on-api", "anthropic-api", "remote-image-urls",
                                           "require-token", "continue-after-gui-exit"])
    set_flag.add_argument("value", choices=["true", "false"])
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
    set_rag = config.add_parser(
        "set-rag", help="ナレッジベース（RAG）の設定（指定したオプションだけ変更）")
    set_rag.add_argument("--enabled", choices=["true", "false"], default=None)
    set_rag.add_argument("--embedding-base-url", default=None)
    set_rag.add_argument("--embedding-model", default=None)
    set_rag.add_argument("--embedding-timeout-seconds", type=int, default=None)
    set_rag.add_argument("--embedding-batch-size", type=int, default=None)
    set_rag.add_argument("--chunk-size", type=int, default=None)
    set_rag.add_argument("--chunk-overlap", type=int, default=None)
    set_rag.add_argument("--default-top-k", type=int, default=None)
    set_rag.add_argument("--max-context-chars", type=int, default=None)
    set_rag.add_argument("--max-chunks-per-collection", type=int, default=None)
    secrets_group = sub.add_parser("secrets").add_subparsers(dest="action", required=True)
    secrets_group.add_parser("get-api-token")
    set_api_token = secrets_group.add_parser("set-api-token"); set_api_token.add_argument("token")
    secrets_group.add_parser("regenerate-api-token")
    secrets_group.add_parser("get-lmstudio-token")
    set_lm_token = secrets_group.add_parser("set-lmstudio-token"); set_lm_token.add_argument("token", nargs="?", default="")
    secrets_group.add_parser("get-rag-embedding-token")
    set_rag_token = secrets_group.add_parser("set-rag-embedding-token")
    set_rag_token.add_argument("token", nargs="?", default="")
    rag = sub.add_parser("rag").add_subparsers(dest="action", required=True)
    rag.add_parser("status")
    rag_collection = rag.add_parser("collection").add_subparsers(dest="sub", required=True)
    rag_collection.add_parser("list")
    rc_create = rag_collection.add_parser("create"); rc_create.add_argument("name")
    rc_delete = rag_collection.add_parser("delete"); rc_delete.add_argument("name")
    rag_doc = rag.add_parser("doc").add_subparsers(dest="sub", required=True)
    rd_add = rag_doc.add_parser("add")
    rd_add.add_argument("collection")
    rd_add.add_argument("--file", default=None)
    rd_add.add_argument("--text", default=None)
    rd_add.add_argument("--title", default=None)
    rd_add.add_argument("--wait", action="store_true")
    rd_list = rag_doc.add_parser("list"); rd_list.add_argument("collection")
    rd_remove = rag_doc.add_parser("remove")
    rd_remove.add_argument("collection"); rd_remove.add_argument("document_id")
    rag_query = rag.add_parser("query")
    rag_query.add_argument("collection"); rag_query.add_argument("query")
    rag_query.add_argument("--top-k", type=int, default=None)
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
    pcache = sub.add_parser("prompt-cache").add_subparsers(dest="action", required=True)
    pcache.add_parser("status")
    pcache.add_parser("clear-memory")
    pcache.add_parser("clear-disk")
    pcache_set = pcache.add_parser("set", help="永続（ディスク）プロンプトキャッシュの設定")
    pcache_set.add_argument("--disk-enabled", choices=["true", "false"], default=None)
    pcache_set.add_argument("--max-gb", type=int, default=None)
    lmstudio = sub.add_parser("lmstudio").add_subparsers(dest="action", required=True)
    lm_base = lmstudio.add_parser("set-base-url"); lm_base.add_argument("url")
    lm_auto = lmstudio.add_parser("set-auto-load"); lm_auto.add_argument("value", choices=["true", "false"])
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
        if args.action == "unload":
            if getattr(args, "model_id", None):
                path = f"/api/v1/models/{args.model_id}/unload"
                if args.force:
                    path += "?force=true"
                return client.request("POST", path).json()
            # `--force` also applies to the all-models unload (matches the GUI's
            # "unload anyway" path); without it the coordinator refuses while a
            # generation is in flight.
            path = "/api/v1/models/loaded" + ("?force=true" if args.force else "")
            return client.request("DELETE", path).json()
        if args.action == "resident":
            status = client.request("GET", "/api/v1/status").json()
            pool = status.get("modelPool", {})
            return {"residentCount": pool.get("residentCount", 0),
                    "residentModelCount": pool.get("residentModelCount"),
                    "maxResidentModels": pool.get("maxResidentModels"),
                    "maxReplicasPerModel": pool.get("maxReplicasPerModel"),
                    "generationConcurrency": pool.get("generationConcurrency"),
                    "configuredGenerationConcurrency": pool.get("configuredGenerationConcurrency"),
                    "effectiveGenerationConcurrency": pool.get("effectiveGenerationConcurrency"),
                    "restartRequired": pool.get("restartRequired"),
                    "activeGenerations": pool.get("activeGenerations"),
                    "replicaShortfalls": pool.get("replicaShortfalls", []),
                    "models": [{"id": item.get("id"), "engine": item.get("engine"),
                                "poolState": item.get("poolState"),
                                "replicaIndex": item.get("replicaIndex"),
                                "replicaCount": item.get("replicaCount"),
                                "readyReplicaCount": item.get("readyReplicaCount"),
                                "desiredReplicaCount": item.get("desiredReplicaCount"),
                                "replicaShortfall": item.get("replicaShortfall"),
                                "keepLoaded": item.get("keepLoaded"),
                                "activeLeases": item.get("activeLeases"),
                                "laneQueueDepth": item.get("laneQueueDepth")}
                               for item in status.get("loadedModels", [])]}
        if args.action in {"pin", "unpin"}:
            settings = client.request("GET", "/api/v1/settings").json()
            pool = settings.get("models", {}).get("pool", {})
            profiles = [dict(item) for item in pool.get("profiles", [])]
            profiles = [item for item in profiles if item.get("modelId") != args.model_id]
            if args.action == "pin":
                profile = {"modelId": args.model_id, "keepLoaded": True}
                if getattr(args, "max_memory_gb", None) is not None:
                    profile["maxMemoryGB"] = args.max_memory_gb
                if getattr(args, "replicas", None) is not None:
                    profile["replicas"] = args.replicas
                profiles.append(profile)
            client.request("PUT", "/api/v1/settings",
                           {"models": {"pool": {"profiles": profiles}}})
            result = {"profiles": profiles,
                      "note": "常駐指定は次回サービス起動時のプリロードに反映されます"}
            if args.action == "pin":
                try:
                    result["loaded"] = client.request(
                        "POST", f"/api/v1/models/{args.model_id}/load", {"engine": "auto"}).json()
                except Exception as exc:  # noqa: BLE001 - report, do not abort the pin
                    result["loadError"] = str(exc)
            return result
        if args.action == "set-replicas":
            if not 1 <= args.count <= 8:
                raise ValueError("並列数（replicas）は1〜8で指定してください")
            settings = client.request("GET", "/api/v1/settings").json()
            pool = settings.get("models", {}).get("pool", {})
            profiles = [dict(item) for item in pool.get("profiles", [])]
            matched = False
            for profile in profiles:
                if profile.get("modelId") == args.model_id:
                    profile["replicas"] = args.count
                    matched = True
            if not matched:
                # Mirror the GUI: an unpinned model gets pinned as it gains a
                # replica count (replicas only apply to a resident model).
                profiles.append({"modelId": args.model_id, "keepLoaded": True,
                                 "replicas": args.count})
            client.request("PUT", "/api/v1/settings",
                           {"models": {"pool": {"profiles": profiles}}})
            return {"profiles": profiles,
                    "note": "並列数の変更は次回サービス起動時、またはreaperによる補充時に反映されます"}
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
        if args.action == "set-model-pool":
            pool: dict = {}
            if args.enabled is not None:
                pool["enabled"] = args.enabled == "true"
            if args.max_resident is not None:
                if not 1 <= args.max_resident <= 8:
                    raise ValueError("最大常駐モデル数は1〜8で指定してください")
                pool["maxResidentModels"] = args.max_resident
            if args.idle_ttl_seconds is not None:
                if not 30 <= args.idle_ttl_seconds <= 86400:
                    raise ValueError("非固定モデルの待機時間は30〜86,400秒で指定してください")
                pool["idleTTLSeconds"] = args.idle_ttl_seconds
            if args.per_model_gb is not None:
                if not 1 <= args.per_model_gb <= 512:
                    raise ValueError("モデルごとの上限は1〜512 GBで指定してください")
                pool["defaultPerModelMaxGB"] = args.per_model_gb
            if args.total_memory_percent is not None:
                if not 50 <= args.total_memory_percent <= 90:
                    raise ValueError("全体メモリ上限は50〜90%で指定してください")
                pool["totalMemoryRatio"] = args.total_memory_percent / 100
            if args.system_reserve_gb is not None:
                if not 1 <= args.system_reserve_gb <= 128:
                    raise ValueError("システム用に残すメモリは1〜128 GBで指定してください")
                pool["minimumSystemReserveGB"] = args.system_reserve_gb
            if args.generation_concurrency is not None:
                if not 1 <= args.generation_concurrency <= 8:
                    raise ValueError("同時生成の上限は1〜8で指定してください")
                pool["generationConcurrency"] = args.generation_concurrency
            if args.max_replicas_per_model is not None:
                if not 1 <= args.max_replicas_per_model <= 8:
                    raise ValueError("モデルごとの並列数上限は1〜8で指定してください")
                pool["maxReplicasPerModel"] = args.max_replicas_per_model
            if not pool:
                raise ValueError("変更するオプションを1つ以上指定してください")
            return client.request("PUT", "/api/v1/settings", {"models": {"pool": pool}}).json()
        if args.action == "set-context-compression":
            # Mirror the GUI's Settings > Models panel (setContextCompressionSettings):
            # the panel shows a percentage, the stored key is a 0..1 ratio.
            patch: dict = {}
            if args.enabled is not None:
                patch["enabled"] = args.enabled == "true"
            if args.trigger_percent is not None:
                if not 50 <= args.trigger_percent <= 95:
                    raise ValueError("発火の目安は50〜95%で指定してください")
                patch["triggerRatio"] = args.trigger_percent / 100
            if args.keep_tail is not None:
                if not 2 <= args.keep_tail <= 50:
                    raise ValueError("要約後も残す直近ターン数は2〜50で指定してください")
                patch["keepTailMessages"] = args.keep_tail
            if args.summary_max_tokens is not None:
                if not 100 <= args.summary_max_tokens <= 4000:
                    raise ValueError("要約の最大トークン数は100〜4,000で指定してください")
                patch["summaryMaxTokens"] = args.summary_max_tokens
            if not patch:
                raise ValueError("変更するオプションを1つ以上指定してください")
            return client.request("PUT", "/api/v1/settings", {"contextCompression": patch}).json()
        if args.action == "set-adaptive-memory":
            # Mirror the GUI's Settings > Experimental panel
            # (setAdaptiveMemorySettings). Ranges match settings.py's _validate;
            # the server also rejects enabling this while contextCompression is on.
            patch: dict = {}
            if args.enabled is not None:
                patch["enabled"] = args.enabled == "true"
            if args.policy is not None:
                patch["policy"] = args.policy
            if args.trigger_percent is not None:
                if not 50 <= args.trigger_percent <= 95:
                    raise ValueError("発火の目安は50〜95%で指定してください")
                patch["triggerRatio"] = args.trigger_percent / 100
            if args.memory_pressure_percent is not None:
                if not 50 <= args.memory_pressure_percent <= 95:
                    raise ValueError("メモリ逼迫の目安は50〜95%で指定してください")
                patch["memoryPressureRatio"] = args.memory_pressure_percent / 100
            if args.keep_tail is not None:
                if not 2 <= args.keep_tail <= 50:
                    raise ValueError("EXACTのまま残す直近ターン数は2〜50で指定してください")
                patch["keepTailMessages"] = args.keep_tail
            if args.max_latent_tokens is not None:
                if not 16 <= args.max_latent_tokens <= 2048:
                    raise ValueError("LATENTブロックの最大ソフトトークン数は16〜2048で指定してください")
                patch["maxLatentTokens"] = args.max_latent_tokens
            if args.max_retrieved_segments is not None:
                if not 1 <= args.max_retrieved_segments <= 64:
                    raise ValueError("取得するLATENTセグメントの上限は1〜64で指定してください")
                patch["maxRetrievedSegments"] = args.max_retrieved_segments
            if args.verbatim_protection is not None:
                patch["verbatimProtection"] = args.verbatim_protection == "true"
            if args.soft_token is not None:
                patch["softToken"] = args.soft_token == "true"
            if not patch:
                raise ValueError("変更するオプションを1つ以上指定してください")
            return client.request("PUT", "/api/v1/settings",
                                  {"experimental": {"adaptiveMemory": patch}}).json()
        if args.action == "set-flag":
            keys = {"auto-load-on-api": "models.autoLoadOnAPIRequest",
                    "anthropic-api": "api.anthropic.enabled",
                    "remote-image-urls": "security.allowRemoteImageUrls",
                    "require-token": "api.requireToken",
                    "continue-after-gui-exit": "general.continueAfterGUIExit"}
            return client.request("PUT", "/api/v1/settings",
                                  nested_patch(keys[args.name], args.value)).json()
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
        if args.action == "set-rag":
            # Mirror the GUI's Settings > Knowledge base panel: only the given
            # options change; ranges match settings.py's _validate.
            patch: dict = {}
            embedding: dict = {}
            if args.enabled is not None:
                patch["enabled"] = args.enabled == "true"
            if args.embedding_base_url is not None:
                if not args.embedding_base_url.startswith(("http://", "https://")):
                    raise ValueError("埋め込みエンドポイントのURLは http:// または https:// で指定してください")
                embedding["baseUrl"] = args.embedding_base_url
            if args.embedding_model is not None:
                embedding["model"] = args.embedding_model
            if args.embedding_timeout_seconds is not None:
                if not 5 <= args.embedding_timeout_seconds <= 300:
                    raise ValueError("埋め込みのタイムアウトは5〜300秒で指定してください")
                embedding["timeoutSeconds"] = args.embedding_timeout_seconds
            if args.embedding_batch_size is not None:
                if not 1 <= args.embedding_batch_size <= 256:
                    raise ValueError("埋め込みのバッチサイズは1〜256で指定してください")
                embedding["batchSize"] = args.embedding_batch_size
            if args.chunk_size is not None:
                if not 100 <= args.chunk_size <= 8000:
                    raise ValueError("チャンクサイズは100〜8,000文字で指定してください")
                patch["chunkSize"] = args.chunk_size
            if args.chunk_overlap is not None:
                if args.chunk_overlap < 0:
                    raise ValueError("チャンクの重なりは0以上で指定してください")
                patch["chunkOverlap"] = args.chunk_overlap
            if args.default_top_k is not None:
                if not 1 <= args.default_top_k <= 20:
                    raise ValueError("既定の取得件数は1〜20で指定してください")
                patch["defaultTopK"] = args.default_top_k
            if args.max_context_chars is not None:
                if not 500 <= args.max_context_chars <= 32000:
                    raise ValueError("注入する文脈の最大文字数は500〜32,000で指定してください")
                patch["maxContextChars"] = args.max_context_chars
            if args.max_chunks_per_collection is not None:
                if not 100 <= args.max_chunks_per_collection <= 50000:
                    raise ValueError("コレクションあたりのチャンク上限は100〜50,000で指定してください")
                patch["maxChunksPerCollection"] = args.max_chunks_per_collection
            if embedding:
                patch["embedding"] = embedding
            if not patch:
                raise ValueError("変更するオプションを1つ以上指定してください")
            return client.request("PUT", "/api/v1/settings", {"rag": patch}).json()
        return client.request("PUT", "/api/v1/settings", nested_patch(args.key, args.value)).json()
    if args.command == "secrets":
        if args.action == "get-api-token": return client.request("GET", "/api/v1/settings/api-token").json()
        if args.action == "set-api-token": return client.request("PUT", "/api/v1/settings/api-token", {"token": args.token}).json()
        if args.action == "regenerate-api-token": return client.request("POST", "/api/v1/settings/api-token/regenerate").json()
        if args.action == "get-lmstudio-token": return client.request("GET", "/api/v1/settings/lm-studio-token").json()
        if args.action == "set-lmstudio-token": return client.request("PUT", "/api/v1/settings/lm-studio-token", {"token": args.token}).json()
        if args.action == "get-rag-embedding-token": return client.request("GET", "/api/v1/settings/rag-embedding-token").json()
        if args.action == "set-rag-embedding-token": return client.request("PUT", "/api/v1/settings/rag-embedding-token", {"token": args.token}).json()
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
    if args.command == "prompt-cache":
        if args.action == "status": return client.request("GET", "/api/v1/prompt-cache").json()
        if args.action == "clear-memory":
            return client.request("POST", "/api/v1/prompt-cache/memory/clear").json()
        if args.action == "clear-disk":
            return client.request("POST", "/api/v1/prompt-cache/disk/clear").json()
        if args.action == "set":
            patch: dict = {}
            if args.disk_enabled is not None:
                patch["diskEnabled"] = args.disk_enabled == "true"
            if args.max_gb is not None:
                if not 1 <= args.max_gb <= 100:
                    raise ValueError("ディスクキャッシュ上限は1〜100 GBで指定してください")
                patch["diskMaxGB"] = args.max_gb
            if not patch:
                raise ValueError("--disk-enabled または --max-gb を指定してください")
            return client.request("PUT", "/api/v1/settings", {"promptCache": patch}).json()
    if args.command == "lmstudio":
        if args.action == "set-base-url":
            return client.request("PUT", "/api/v1/settings",
                                  {"models": {"lmStudio": {"baseUrl": args.url}}}).json()
        if args.action == "set-auto-load":
            return client.request("PUT", "/api/v1/settings",
                                  {"models": {"lmStudio": {"autoLoad": args.value == "true"}}}).json()
    if args.command == "rag":
        if args.action == "status":
            return client.request("GET", "/api/v1/rag/status").json()
        if args.action == "collection":
            if args.sub == "list":
                return client.request("GET", "/api/v1/rag/collections").json()
            if args.sub == "create":
                return client.request("POST", "/api/v1/rag/collections", {"name": args.name}).json()
            if args.sub == "delete":
                return client.request("DELETE", f"/api/v1/rag/collections/{args.name}").json()
        if args.action == "doc":
            if args.sub == "add":
                if bool(args.file) == bool(args.text):
                    raise ValueError("--file または --text のどちらか一方を指定してください")
                body: dict = {"title": args.title}
                if args.file:
                    body["path"] = str(Path(args.file).expanduser())
                else:
                    body["text"] = args.text
                job = client.request(
                    "POST", f"/api/v1/rag/collections/{args.collection}/documents", body).json()
                return wait_job(client, job, args.global_json) if args.wait else job
            if args.sub == "list":
                return client.request(
                    "GET", f"/api/v1/rag/collections/{args.collection}/documents").json()
            if args.sub == "remove":
                return client.request(
                    "DELETE",
                    f"/api/v1/rag/collections/{args.collection}/documents/{args.document_id}").json()
        if args.action == "query":
            payload: dict = {"query": args.query}
            if args.top_k is not None:
                payload["topK"] = args.top_k
            return client.request(
                "POST", f"/api/v1/rag/collections/{args.collection}/query", payload).json()
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
