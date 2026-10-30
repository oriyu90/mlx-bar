"""Base class for the bundled loaders (v3.2.0 / ISS-5).

A bundled loader is *repository* code that knows how to make one non-standard
artifact loadable. It is not a plugin system: the registry in
``loaders/__init__.py`` is closed, model-supplied code is never imported, and a
loader that cannot finish declares ``implemented = False`` so the worker
returns an honest, actionable error instead of trying a doomed 100 GB load.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class LoaderSpec:
    loader_id: str
    display_name: str
    description: str
    model_types: tuple[str, ...] = ()
    config_keys: tuple[str, ...] = ()
    # False means: detection + preflight are implemented, the weight
    # reconstruction is not. `plan()` then refuses with
    # MODEL_NEEDS_BUNDLED_LOADER instead of loading a broken model.
    implemented: bool = False


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    extra: dict = field(default_factory=dict)


class BundledLoader:
    """One bundled loader. Subclasses override `preflight` and `load_impl`."""

    spec: LoaderSpec

    def matches(self, config: dict, path: str | Path | None = None) -> bool:
        """Whether this loader is responsible for the artifact in `config`."""
        model_type = str(config.get("model_type") or "").casefold()
        if model_type and model_type in {value.casefold() for value in self.spec.model_types}:
            return True
        return any(key in config for key in self.spec.config_keys)

    def preflight(self, path: str | Path) -> dict:
        """Validate the artifact without touching weights.

        Returns a report dict. Raises `common.model_errors.ModelLoadError` when
        a check proves the artifact cannot be served by this loader.
        """
        return {"loaderId": self.spec.loader_id, "checks": []}

    def load(self, path: str | Path, *, trust_remote_code: bool = False):
        """Load the model through this loader (only when `implemented`)."""
        from common.model_errors import ModelLoadError

        if not self.spec.implemented:
            raise ModelLoadError(
                "MODEL_NEEDS_BUNDLED_LOADER",
                f"同梱ローダ「{self.spec.display_name}」の重み再構成は未実装です",
                detail=self.spec.description)
        return self.load_impl(Path(path), trust_remote_code=trust_remote_code)

    def load_impl(self, path: Path, *, trust_remote_code: bool = False):
        raise NotImplementedError
