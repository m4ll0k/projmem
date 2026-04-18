"""Entrypoint detection. Heuristic; reports confidence."""
from __future__ import annotations
import os
import re

from .config import Config
from .store import Store
from .utils import read_text


def detect(store: Store, cfg: Config) -> None:
    # Recompute the entire entrypoints table each run. User-declared entries
    # for files outside the indexed tree would otherwise accumulate because
    # `delete_file_data` is only called for files we re-index.
    store.conn.execute("DELETE FROM entrypoints")

    # user-declared, high confidence
    for ep in cfg.entrypoints or []:
        store.add_entrypoint(file=ep, kind="user-declared",
                             confidence="high", evidence="config.entrypoints")

    for row in store.all_files():
        path = row["path"]
        full = os.path.join(cfg.root, path)
        lang = row["lang"]
        name = os.path.basename(path)

        # Strong hints by filename
        if name in ("__main__.py",):
            store.add_entrypoint(file=path, kind="python-main",
                                 confidence="high", evidence="filename")
        if name in ("manage.py", "wsgi.py", "asgi.py", "app.py", "server.py", "main.py", "cli.py"):
            store.add_entrypoint(file=path, kind="python-entry-candidate",
                                 confidence="medium", evidence=f"filename:{name}")
        if name in ("index.js", "index.ts", "server.js", "server.ts", "main.js", "main.ts", "cli.js", "cli.ts"):
            store.add_entrypoint(file=path, kind="js-entry-candidate",
                                 confidence="medium", evidence=f"filename:{name}")

        # Language-specific main() detection (AST would be cleaner; regex is
        # adequate and cross-language). High confidence on exact matches.
        if lang == "python":
            src = read_text(full)
            if re.search(r"""if\s+__name__\s*==\s*['"]__main__['"]""", src):
                store.add_entrypoint(file=path, kind="python-main-guard",
                                     confidence="high", evidence="__main__ guard")
        elif lang == "go":
            src = read_text(full)
            # Only package main + a func main() qualifies as a Go binary entry.
            if re.search(r"^\s*package\s+main\b", src, re.M) and \
               re.search(r"^\s*func\s+main\s*\(\s*\)", src, re.M):
                store.add_entrypoint(file=path, kind="go-main",
                                     confidence="high",
                                     evidence="package main + func main()")
        elif lang in ("c", "cpp"):
            src = read_text(full)
            if re.search(r"^\s*int\s+main\s*\(", src, re.M):
                store.add_entrypoint(file=path, kind=f"{lang}-main",
                                     confidence="high", evidence="int main(")
        elif lang == "java":
            src = read_text(full)
            if re.search(r"public\s+static\s+void\s+main\s*\(\s*String\s*\[\s*\]", src):
                store.add_entrypoint(file=path, kind="java-main",
                                     confidence="high",
                                     evidence="public static void main(String[])")
        elif lang == "rust":
            src = read_text(full)
            # Only in a binary crate (main.rs, or a file with fn main at top-level).
            if re.search(r"^\s*fn\s+main\s*\(\s*\)", src, re.M):
                store.add_entrypoint(file=path, kind="rust-main",
                                     confidence="high", evidence="fn main()")
        elif lang in ("javascript", "typescript"):
            src = read_text(full)
            # Node CommonJS: `if (require.main === module) { ... }`
            if re.search(r"require\.main\s*={2,3}\s*module", src):
                store.add_entrypoint(file=path, kind="node-main-guard",
                                     confidence="high",
                                     evidence="require.main === module")
            # ESM equivalent: `import.meta.url` compared to process argv[1].
            if re.search(r"import\.meta\.url", src) and "process.argv" in src:
                store.add_entrypoint(file=path, kind="node-esm-main-guard",
                                     confidence="medium",
                                     evidence="import.meta.url + process.argv")

        # package.json bin/main
        if name == "package.json":
            import json
            try:
                data = json.loads(read_text(full))
            except Exception:
                data = {}
            for k in ("main", "module"):
                v = data.get(k)
                if isinstance(v, str):
                    ref = os.path.normpath(os.path.join(os.path.dirname(path), v))
                    store.add_entrypoint(file=ref.replace("\\", "/"),
                                         kind=f"package.json:{k}",
                                         confidence="high",
                                         evidence=f"package.json {k}")
            bins = data.get("bin") or {}
            if isinstance(bins, str):
                bins = {"default": bins}
            if isinstance(bins, dict):
                for bname, bv in bins.items():
                    if isinstance(bv, str):
                        ref = os.path.normpath(os.path.join(os.path.dirname(path), bv))
                        store.add_entrypoint(file=ref.replace("\\", "/"),
                                             kind=f"package.json:bin:{bname}",
                                             confidence="high",
                                             evidence="package.json bin")

    # Final safety net: dedupe rows on (file, kind). The `DELETE FROM entrypoints`
    # at the top of this function normally prevents accumulation, but belt &
    # braces here covers re-entries within a single run.
    store.conn.execute(
        "DELETE FROM entrypoints WHERE id NOT IN "
        "(SELECT MIN(id) FROM entrypoints GROUP BY file, kind)"
    )
    # Round-3 report bug #2: entrypoints referencing non-indexed files (e.g.
    # `package.json:main` pointing at an --exclude'd path) look like real
    # graph nodes. Mark `indexed=0` so consumers can see the truth and
    # packs can surface a `file-not-indexed` unknown instead of silently
    # trusting the entry.
    indexed_paths = {r["path"] for r in store.all_files()}
    rows = list(store.conn.execute("SELECT id, file FROM entrypoints"))
    for row in rows:
        is_indexed = 1 if row["file"] in indexed_paths else 0
        store.conn.execute("UPDATE entrypoints SET indexed=? WHERE id=?",
                           (is_indexed, row["id"]))
