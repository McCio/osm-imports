"""Core layer descriptor types — no translate imports, safe to import from anywhere."""

from collections.abc import Callable
from dataclasses import dataclass

from dbsn.common import EXCLUDE_META_IST


@dataclass(frozen=True)
class LayerSpec:
    """Extraction-time descriptor: what to pull from the GDB and where to write FGB."""
    name: str
    filter_fn: Callable | None  # extra filter beyond meta_ist; None = accept all
    supports_extension: bool    # has classid → cross-boundary ext FGBs apply

    def passes_filter(self, props: dict) -> bool:
        if props.get("meta_ist") in EXCLUDE_META_IST:
            return False
        return self.filter_fn is None or self.filter_fn(props)


@dataclass(frozen=True)
class LayerDef(LayerSpec):
    """Full descriptor including translator — used at convert time."""
    translate_fn: Callable  # (attrs: dict, region: str | None = None) -> dict | None
    tag_keys: list[str]
