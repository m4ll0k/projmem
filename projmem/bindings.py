"""Native binding edge extraction.

This module detects explicit JS-facing name ↔ native function bindings in
C/C++ codebases. Primary motivation is Node.js/V8-style registration where
a C++ function is exposed to JS under a string name via helpers like:

  SetMethod(context, target, "internalModuleStat", InternalModuleStat);

We keep the extractor conservative:
  - single-line patterns only (high precision)
  - require an explicit string literal + identifier in the same call
  - js_name must look like a plain identifier
  - no speculative fuzzy matching
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple


_JS_IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

# C++ function identifier (optionally qualified). We capture only the lexical
# name; resolution to a concrete symbol_id is done by the indexer/store layer.
_CPP_IDENT = r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*"


_PATTERNS: List[Tuple[str, float, re.Pattern[str]]] = [
    (
        "SetMethod binding",
        0.9,
        re.compile(
            rf"\bSetMethod(?:NoSideEffect)?\s*\([^;]*?['\"](?P<js>[^'\"]+)['\"]\s*,\s*&?(?P<cpp>{_CPP_IDENT})\s*\)"
        ),
    ),
    (
        "SetPrototypeMethod binding",
        0.9,
        re.compile(
            rf"\bSetPrototypeMethod(?:NoSideEffect)?\s*\([^;]*?['\"](?P<js>[^'\"]+)['\"]\s*,\s*&?(?P<cpp>{_CPP_IDENT})\s*\)"
        ),
    ),
    (
        "NODE_SET_METHOD binding",
        0.95,
        re.compile(
            rf"\bNODE_SET_METHOD\s*\([^;]*?['\"](?P<js>[^'\"]+)['\"]\s*,\s*&?(?P<cpp>{_CPP_IDENT})\s*\)"
        ),
    ),
]


def extract_cpp_bindings(src: str) -> List[Dict[str, object]]:
    """Return a list of explicit binding edges found in `src`.

    Each row has:
      line, js_name, cpp_name, confidence, reason, evidence
    """
    out: List[Dict[str, object]] = []
    seen: set = set()
    for line_no, line in enumerate((src or "").splitlines(), start=1):
        if ("\"" not in line and "'" not in line) or "(" not in line:
            continue
        for reason, conf, rx in _PATTERNS:
            m = rx.search(line)
            if not m:
                continue
            js_name = (m.group("js") or "").strip()
            cpp_name = (m.group("cpp") or "").strip()
            if not (_JS_IDENT.match(js_name) and cpp_name):
                continue
            key = (line_no, js_name, cpp_name, reason)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "line": line_no,
                "js_name": js_name,
                "cpp_name": cpp_name,
                "confidence": float(conf),
                "reason": reason,
                "evidence": line.strip()[:240],
            })
    return out

