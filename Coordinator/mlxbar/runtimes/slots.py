from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


class SlotStore:
    def __init__(self, root: Path):
        self.root = root / "runtimes"
        self.root.mkdir(parents=True, exist_ok=True)

    def engine_root(self, engine: str) -> Path:
        if engine not in {"mlx-lm", "mlx-vlm"}:
            raise ValueError("invalid engine")
        path = self.root / engine
        (path / "slots").mkdir(parents=True, exist_ok=True)
        return path

    def active(self, engine: str) -> dict:
        path = self.engine_root(engine) / "active.json"
        if not path.exists():
            return {"active": None, "previous": None}
        return json.loads(path.read_text(encoding="utf-8"))

    def activate(self, engine: str, slot_id: str) -> dict:
        root = self.engine_root(engine)
        slot = root / "slots" / slot_id
        probe = slot / "probe.json"
        if not probe.exists() or not json.loads(probe.read_text()).get("compatible"):
            raise ValueError("slot is not verified")
        current = self.active(engine)
        data = {"active": slot_id, "previous": current.get("active")}
        temp = root / "active.tmp"
        temp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(temp, root / "active.json")
        return data

    def rollback(self, engine: str) -> dict:
        current = self.active(engine)
        if not current.get("previous"):
            raise ValueError("no previous slot")
        return self.activate(engine, current["previous"])

    def delete(self, engine: str, slot_id: str) -> dict:
        current = self.active(engine)
        if current.get("active") == slot_id:
            raise ValueError("使用中のslotは削除できません")
        slots = {item["id"]: item for item in self.list(engine)}
        if slot_id not in slots:
            raise ValueError("slotが見つかりません")
        slot = self.engine_root(engine) / "slots" / slot_id
        size_bytes = sum(path.stat().st_size for path in slot.rglob("*") if path.is_file())
        version = slots[slot_id].get("probe", {}).get("version")
        was_previous = current.get("previous") == slot_id
        shutil.rmtree(slot)
        if was_previous:
            data = {"active": current.get("active"), "previous": None}
            temp = self.engine_root(engine) / "active.tmp"
            temp.write_text(json.dumps(data, indent=2) + "\n")
            os.replace(temp, self.engine_root(engine) / "active.json")
        return {"engine": engine, "slotId": slot_id, "version": version,
                "sizeBytes": size_bytes, "removedPrevious": was_previous}

    def restore(self, engine: str, slot_id: str | None) -> dict:
        root = self.engine_root(engine)
        active_path = root / "active.json"
        if slot_id is None:
            active_path.unlink(missing_ok=True)
            return {"active": None, "previous": None}
        slot = root / "slots" / slot_id
        probe = slot / "probe.json"
        if not probe.exists() or not json.loads(probe.read_text()).get("compatible"):
            raise ValueError("previous slot is not verified")
        data = {"active": slot_id, "previous": None}
        temp = root / "active.tmp"
        temp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(temp, active_path)
        return data

    def list(self, engine: str) -> list[dict]:
        active = self.active(engine)
        result = []
        for path in (self.engine_root(engine) / "slots").iterdir():
            if not path.is_dir():
                continue
            manifest = path / "manifest.json"
            probe = path / "probe.json"
            result.append({"id": path.name, "active": path.name == active.get("active"),
                           "previous": path.name == active.get("previous"),
                           "manifest": json.loads(manifest.read_text()) if manifest.exists() else {},
                           "probe": json.loads(probe.read_text()) if probe.exists() else {}})
        return sorted(result, key=lambda item: item["id"], reverse=True)

    def cleanup(self, engine: str, keep: int = 3) -> list[str]:
        keep = max(2, keep)
        active = self.active(engine)
        protected = {item for item in (active.get("active"), active.get("previous")) if item}
        slots = [item["id"] for item in self.list(engine)]
        retained = set(protected)
        for slot_id in slots:
            if len(retained) >= keep:
                break
            retained.add(slot_id)
        removed = []
        root = self.engine_root(engine) / "slots"
        for slot_id in slots:
            if slot_id not in retained:
                shutil.rmtree(root / slot_id, ignore_errors=True)
                removed.append(slot_id)
        return removed
