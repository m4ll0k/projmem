"""Config & optional user-declared contract rules.

Loads `.projmem/config.json` (or YAML if pyyaml available) from the project root.
Schema (all optional):

{
  "include_globs": ["**/*.py", "**/*.js"],
  "exclude_globs": ["**/vendor/**"],
  "max_file_bytes": 1000000,
  "entrypoints": ["src/cli.py", "src/server.py"],
  "contracts": {
    "flags": ["--live-logs"],
    "env": ["DATABASE_URL"],
    "schema_fields": ["status", "evidence"],
    "tokens": ["IN_PROGRESS", "DONE"],
    "pairs": [{"if_touch": "src/auth.py", "inspect": ["tests/test_auth.py"]}]
  }
}
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class Config:
    root: str
    include_globs: List[str] = field(default_factory=list)
    exclude_globs: List[str] = field(default_factory=list)
    max_file_bytes: int = 3_000_000  # 3MB — covers ~30k-line JS monoliths
    entrypoints: List[str] = field(default_factory=list)
    contracts: Dict[str, Any] = field(default_factory=dict)

    @property
    def store_dir(self) -> str:
        return os.path.join(self.root, ".projmem")

    @property
    def db_path(self) -> str:
        return os.path.join(self.store_dir, "index.db")

    @property
    def packs_dir(self) -> str:
        return os.path.join(self.store_dir, "packs")


def load(root: str) -> Config:
    root = os.path.abspath(root)
    cfg = Config(root=root)
    for name in ("config.json", "config.yaml", "config.yml"):
        p = os.path.join(root, ".projmem", name)
        if os.path.isfile(p):
            data = _read(p)
            if not isinstance(data, dict):
                continue
            for k in ("include_globs", "exclude_globs", "entrypoints"):
                if isinstance(data.get(k), list):
                    setattr(cfg, k, data[k])
            if isinstance(data.get("max_file_bytes"), int):
                cfg.max_file_bytes = data["max_file_bytes"]
            if isinstance(data.get("contracts"), dict):
                cfg.contracts = data["contracts"]
            break
    return cfg


def _read(path: str):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except Exception:
        return None
