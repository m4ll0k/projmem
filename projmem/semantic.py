"""Semantic contract layer.

Detects (heuristically, with explicit confidence):
  - CLI flags: --name tokens and argparse/click/commander parse sites
  - env vars: os.environ/os.getenv in Python; process.env.X in JS/TS
  - schema fields / object keys: repeated string literals used as dict/obj keys
  - tokens: repeated string literals across >=2 files

User-declared contract rules (from .projmem/config.json) are added with high confidence.
"""
from __future__ import annotations
import ast
import re
from collections import defaultdict
from typing import Dict, List

from .config import Config
from .store import Store


FLAG_RX = re.compile(r"(?<![A-Za-z0-9])--([a-z][a-z0-9][a-z0-9\-]{1,40})")
PY_ENV_RX = re.compile(r"""os\.(?:environ(?:\.get)?|getenv)\s*[\[\(]\s*['"]([A-Z_][A-Z0-9_]{1,64})['"]""")
JS_ENV_RX = re.compile(r"""process\.env\.([A-Z_][A-Z0-9_]{1,64})""")
JS_ENV_RX2 = re.compile(r"""process\.env\[\s*['"]([A-Z_][A-Z0-9_]{1,64})['"]""")

# Computed env access — `process.env[variable]` or `process.env[fn(...)]`.
# We can't know the runtime value, but recording the OCCURRENCE lets
# claim verification know "a literal scan at this site is incomplete; a
# computed access can touch env names the literal scan can't see".
# Match only when the bracket content is NOT a literal string (literal
# case is caught by JS_ENV_RX2 above).
JS_ENV_COMPUTED_RX = re.compile(
    r"""process\.env\[\s*(?!['"])([A-Za-z_$][\w$]*)"""
)
JS_ENV_COMPUTED_CALL_RX = re.compile(
    r"""process\.env\[\s*(?!['"])(\w+)\s*\("""
)
# env.FOO / env['FOO'] when `env` is clearly a captured alias to
# process.env (`const env = process.env;`) — common in large codebases.
# Too broad in general, so we ALSO require `env` to have been aliased
# earlier in the file (cheap scan below).
JS_ENV_ALIAS_RX = re.compile(
    r"""\b([A-Za-z_$][\w$]*)\.env\.([A-Z_][A-Z0-9_]{1,64})"""
)
# Schema-library declarations. These are DECLARATIONS (not reads); we
# tag them role='declare' so the contract diff / claim verifier can
# tell the difference between "this env is declared in schema" and
# "this env is read at line X".
#
# Supports: Zod object, @t3-oss/env-core, envalid, drizzle/zod schemas.
# Patterns are deliberately conservative — require SHOUTY_CASE env names
# and common surrounding cues.
_ZOD_ENV_SCHEMA_BLOCK = re.compile(
    r"""z\.object\s*\(\s*\{([^}]{1,4000})\}""",
    re.DOTALL,
)
_T3_ENV_BLOCK = re.compile(
    r"""createEnv\s*\(\s*\{([^}]{1,4000})\}""",
    re.DOTALL,
)
_ENVALID_BLOCK = re.compile(
    r"""cleanEnv\s*\(\s*process\.env\s*,\s*\{([^}]{1,4000})\}""",
    re.DOTALL,
)
# Inside a schema block, extract `FOO: <validator>` pairs.
_SCHEMA_FIELD_RX = re.compile(
    r"""(?:^|\s|,)([A-Z_][A-Z0-9_]{1,64})\s*:""",
    re.MULTILINE,
)
GO_ENV_RX = re.compile(r"""os\.(?:Getenv|LookupEnv)\(\s*"([A-Z_][A-Z0-9_]{1,64})"\s*\)""")
RUST_ENV_RX = re.compile(r"""env::(?:var|var_os)\(\s*"([A-Z_][A-Z0-9_]{1,64})"\s*\)""")
C_ENV_RX = re.compile(r"""getenv\(\s*"([A-Z_][A-Z0-9_]{1,64})"\s*\)""")
# Real-world C/C++ env access on top of bare getenv(). Patterns gathered
# from nodejs/node v22.11.0 (docs/REAL_STRESS_FINAL.md §5):
#   SafeGetenv("NODE_OPTIONS", ...)
#   credentials::SafeGetenv("X", ...)
#   env_vars->Get("X")     / env_vars().Get("X")  / env_vars()->Get("X")
#   environment->Get("X")
#   GetEnvironmentVariableA/W("X", ...)            (Win32)
# Restricted to capitalised env-name shape so an arbitrary
# `something->Get("Foo")` doesn't poison the env contract space.
C_ENV_SAFEGETENV_RX = re.compile(
    r"""\b(?:[A-Za-z_:][A-Za-z0-9_:]*::)?(?:Safe)?[Gg]etenv\(\s*"([A-Z_][A-Z0-9_]{1,64})"\s*[,)]""")
C_ENV_VARS_GET_RX = re.compile(
    r"""\benv_vars\s*(?:\(\)\s*)?(?:->|\.)\s*(?:Get|Find|Has)\(\s*"([A-Z_][A-Z0-9_]{1,64})"\s*\)""")
C_ENVIRONMENT_GET_RX = re.compile(
    r"""\b(?:environment|process_env|env_)(?:->|\.)\s*Get\(\s*"([A-Z_][A-Z0-9_]{1,64})"\s*\)""")
C_WIN32_ENV_RX = re.compile(
    r"""\bGetEnvironmentVariable[AW]?\(\s*L?"([A-Z_][A-Z0-9_]{1,64})"\s*[,)]""")
JAVA_ENV_RX = re.compile(r"""System\.getenv\(\s*"([A-Z_][A-Z0-9_]{1,64})"\s*\)""")
# Java Bean setter — `obj.setFooBar(value)` / `setFooBar(value)`. The flag
# name we record is the camelCase tail (FooBar -> fooBar). Tomcat-style
# config (`connector.setAllowBackslash(true)`) lives in this shape and was
# previously invisible to `projmem flow` — fact-check / drift could never
# answer "who flips this flag?" Captures argument literally so a future
# enhancement can read `value` for true/false split.
JAVA_BEAN_SETTER_RX = re.compile(
    r"""
    (?:                              # call target — optional
        (?:[A-Za-z_$][\w$]*\.)+      # `obj.` or `pkg.cls.`
    |   \b                            # bare `setXxx(...)`
    )
    set([A-Z][A-Za-z0-9_]{1,40})     # capture: PascalCase suffix
    \s*\(                             # opening paren
    \s*([^);,]{0,80})                 # capture: first arg up to , or )
    """,
    re.VERBOSE,
)
# Sites mentioning a setter name in XML / Spring config — the property
# attribute on a `<bean>` / `<set-property>` element. Flag name lives
# verbatim. Captured at confidence=medium because XML schemas are
# project-specific (Spring vs Tomcat vs Camel all spell it differently).
JAVA_XML_SETTER_RX = re.compile(
    r"""<(?:property|set-property|attribute)\s+[^>]*\bname\s*=\s*"([a-z][A-Za-z0-9_]{1,40})\"""",
    re.VERBOSE,
)
RUBY_ENV_RX = re.compile(r"""ENV\[\s*['"]([A-Z_][A-Z0-9_]{1,64})['"]\s*\]""")
# Go / Rust struct tags: `json:"field_name"` or `serde(rename = "field_name")`.
# Captured as schema_field WRITES because serialization emits the field.
GO_STRUCT_TAG_RX = re.compile(r"""`[^`]*\bjson:\s*"([A-Za-z_][A-Za-z0-9_]*)(?:,[^"]*)?"[^`]*`""")
RUST_SERDE_RX = re.compile(r'#\[serde\s*\(\s*rename\s*=\s*"([A-Za-z_][A-Za-z0-9_]*)"')

# Event / listener pairs — essential for catching temporal-coupling bugs
# (e.g. listener registered AFTER the emitting call). We capture the event
# name (string literal) and whether the site is an emitter or a listener.
EV_LISTEN_RX = re.compile(
    r"""(?:\.|^|\s)(?:on|once|addListener|prependListener|addEventListener)"""
    r"""\s*\(\s*['"]([A-Za-z_][\w.:\-]{0,80})['"]""",
    re.MULTILINE,
)
EV_EMIT_RX = re.compile(
    r"""(?:\.|^|\s)(?:emit|dispatchEvent|fireEvent|trigger)"""
    r"""\s*\(\s*['"]([A-Za-z_][\w.:\-]{0,80})['"]""",
    re.MULTILINE,
)
EV_DISPATCH_NEW_RX = re.compile(
    r"""dispatchEvent\s*\(\s*new\s+(?:Custom)?Event\s*\(\s*['"]([A-Za-z_][\w.:\-]{0,80})['"]""",
    re.MULTILINE,
)
JS_OBJ_KEY_RX = re.compile(r"""['"]([A-Za-z_][A-Za-z0-9_]{1,32})['"]\s*:""")
JS_INDEX_KEY_RX = re.compile(r"""\[\s*['"]([A-Za-z_][A-Za-z0-9_]{1,32})['"]\s*\]""")
STRING_LITERAL_RX = re.compile(r"""(?<!\\)(?:'([^'\\\n]{2,40})'|"([^"\\\n]{2,40})")""")
ARGPARSE_ADD_RX = re.compile(r"""add_argument\(\s*['"]--([a-z][a-z0-9\-_]{1,40})['"]""")
CLICK_OPTION_RX = re.compile(r"""@click\.option\(\s*['"]--([a-z][a-z0-9\-_]{1,40})['"]""")
COMMANDER_OPT_RX = re.compile(r"""\.option\(\s*['"]-\w,\s*--([a-z][a-z0-9\-_]{1,40})['"]""")


# Assignment-to-attribute: `obj.field = ...` or `obj.sub.field = ...`.
# Important: the USER report showed this pattern was silently missed, so
# literal `{status: 'clean'}` captured 0 writes while `result.status = 'clean'`
# appeared at 6+ sites. We now record it as a high-medium confidence write.
JS_ATTR_ASSIGN_RX = re.compile(
    r"""\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.([A-Za-z_][A-Za-z0-9_]{1,40})\s*="""
    r"""(?!=)"""  # not `==` or `===`
)


def _collect_guarded_calls_python(tree: ast.AST):
    """Best-effort guard extraction for Python: yield (caller_if_known, callee,
    condition_text, line). Lightweight — a down-payment on H2 (reachability).

    We emit a guard when a `call` node lives inside an `if` test branch; the
    condition is the textual form of the `if` expression. Not a full
    reachability analysis — just "this call fires only under this textual
    condition", which is already enough to answer "under what condition does
    _runHashSinkProbe fire?" for many real cases.
    """
    import ast as _ast
    out = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.If):
            continue
        cond_text = None
        try:
            cond_text = _ast.unparse(node.test)  # py3.9+
        except Exception:
            cond_text = None
        if not cond_text:
            continue
        for sub in _ast.walk(node):
            if isinstance(sub, _ast.Call):
                callee = None
                fn = sub.func
                if isinstance(fn, _ast.Name):
                    callee = fn.id
                elif isinstance(fn, _ast.Attribute):
                    callee = fn.attr
                if callee and len(callee) >= 2:
                    out.append((callee, cond_text[:200], sub.lineno))
    return out


def scan_python_contracts(store: Store, rel_path: str, tree: ast.AST, src: str) -> None:
    # argparse / click declarations
    for m in ARGPARSE_ADD_RX.finditer(src):
        store.add_contract(kind="flag", name=m.group(1), file=rel_path,
                           line=src.count("\n", 0, m.start()) + 1,
                           role="parse", confidence="high", context="argparse")
    for m in CLICK_OPTION_RX.finditer(src):
        store.add_contract(kind="flag", name=m.group(1), file=rel_path,
                           line=src.count("\n", 0, m.start()) + 1,
                           role="parse", confidence="high", context="click")

    # --flag tokens anywhere (medium confidence: could be docstring/comment)
    for m in FLAG_RX.finditer(src):
        store.add_contract(kind="flag", name=m.group(1), file=rel_path,
                           line=src.count("\n", 0, m.start()) + 1,
                           role="occurrence", confidence="medium",
                           context="token")

    # env vars
    for m in PY_ENV_RX.finditer(src):
        store.add_contract(kind="env", name=m.group(1), file=rel_path,
                           line=src.count("\n", 0, m.start()) + 1,
                           role="read", confidence="high", context="os.environ/getenv")

    # Dict-like string-key reads/writes and subscript access via AST
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str):
            k = node.slice.value
            if _looks_like_field(k):
                role = "write" if isinstance(node.ctx, ast.Store) else "read"
                store.add_contract(kind="schema_field", name=k, file=rel_path,
                                   line=node.lineno, role=role,
                                   confidence="medium", context="subscript")
        if isinstance(node, ast.Call):
            # d.get("k"), d.pop("k")
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "pop", "setdefault") \
                    and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                k = node.args[0].value
                if _looks_like_field(k):
                    store.add_contract(kind="schema_field", name=k, file=rel_path,
                                       line=node.lineno,
                                       role="read" if node.func.attr == "get" else "write",
                                       confidence="medium", context=f".{node.func.attr}()")
        # dict literals with string keys (write)
        if isinstance(node, ast.Dict):
            for kn in node.keys:
                if isinstance(kn, ast.Constant) and isinstance(kn.value, str):
                    k = kn.value
                    if _looks_like_field(k):
                        store.add_contract(kind="schema_field", name=k, file=rel_path,
                                           line=kn.lineno, role="write",
                                           confidence="medium", context="dict-literal")
        # Attribute-assignment writes: `obj.field = X`, `self.field = X` —
        # the dominant write pattern in many Python codebases.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    k = target.attr
                    if _looks_like_field(k):
                        store.add_contract(
                            kind="schema_field", name=k, file=rel_path,
                            line=target.lineno, role="write_assign",
                            confidence="high", context="attr-assign")
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
            k = node.target.attr
            if _looks_like_field(k):
                store.add_contract(
                    kind="schema_field", name=k, file=rel_path,
                    line=node.target.lineno, role="write_assign",
                    confidence="high", context="attr-augassign")

    # H2 (lightweight): guard edges for Python. Record each call whose enclosing
    # `if` condition can be extracted as text. Stored as contract kind='guard'
    # so the query path is uniform; `projmem reach <callee>` can join on this.
    for callee, cond, line in _collect_guarded_calls_python(tree):
        store.add_contract(
            kind="guard", name=callee, file=rel_path, line=line,
            role="guard", confidence="medium", context=cond)

    # Flag-READ detection. argparse/click dests are accessed as
    # `args.<dest>` / `opts.<dest>` / `options.<dest>` / `parsed.<dest>`.
    # Emitting these as `kind=flag, role=read` contracts lets
    # contract-diff's consumer analysis answer "who reads --new-flag?"
    # without needing a tree-sitter backend. Bare (args, opts) naming is
    # the common convention; other callers get picked up by the name
    # probes in contract_diff._consumer_analysis.
    _FLAG_HOLDER_NAMES = {"args", "opts", "options", "parsed", "ns"}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)):
            continue
        if node.value.id not in _FLAG_HOLDER_NAMES:
            continue
        if not _looks_like_field(node.attr):
            continue
        store.add_contract(
            kind="flag", name=node.attr, file=rel_path,
            line=node.lineno, role="read", confidence="medium",
            context=f"{node.value.id}.{node.attr}")

    # Event emit/listen pairs. Python codebases implement pub-sub via the
    # exact `bus.on(...)` / `bus.emit(...)` shape JS uses (Django signals,
    # blinker, pyee, custom dispatchers), so the same regexes are correct
    # here. Without this, `projmem events` missed every Python emitter /
    # listener, which is the headline signal for temporal-pair bugs like
    # listener-registered-for-typo'd-event-name.
    for m in EV_LISTEN_RX.finditer(src):
        store.add_contract(
            kind="event", name=m.group(1), file=rel_path,
            line=src.count("\n", 0, m.start()) + 1,
            role="listen", confidence="medium", context="event-on")
    for m in EV_EMIT_RX.finditer(src):
        store.add_contract(
            kind="event", name=m.group(1), file=rel_path,
            line=src.count("\n", 0, m.start()) + 1,
            role="emit", confidence="medium", context="event-emit")

    _scan_tokens(store, rel_path, src)


def _mask_comments_and_strings(src: str, lang: str,
                                 mask_strings: bool = True) -> str:
    """Return `src` with comment (and optionally string-literal) spans
    replaced by spaces of the same length.

    Audit P0#1: the regex contract extractor used to match `process.env.X`
    inside `// comments`, `/* block comments */`, and `'string literals'`,
    silently turning documentation chatter into "real" env-read contracts.
    Claim verification then VERIFIED claims that pointed at strings
    that were never executed — the worst possible failure for a
    drift-aware memory product.

    Audit P1#4: but token extraction (`_scan_tokens`) DEPENDS on string
    literals — that's where Prisma enum string values like
    `'WEBHOOK_FAILED'` live. So masking is parametric: env-style
    extractors call with `mask_strings=True`; token extraction calls
    with `mask_strings=False` so it keeps seeing string contents.
    Comments are ALWAYS masked because no legitimate contract lives in
    a comment.

    Strategy: walk `src` character-by-character with a tiny state
    machine, tracking whether we're inside a comment or string. We
    REPLACE the masked span with spaces of equal length so that:
      (a) downstream regex offsets stay aligned with line numbers
          (no off-by-one on src.count("\\n", 0, m.start()))
      (b) regex matchers inside masked spans never fire — spaces don't
          satisfy any of the env / flag patterns

    Lang-aware: which comment / string syntax we recognize depends on
    the parser language. JS/TS handle line/block comments + single,
    double, and template (backtick) strings. Python adds line comments
    plus single, double, and triple-quoted strings. C/C++/Go/Rust/Java
    handle line + block comments + single + double quotes. Conservative
    on languages we don't know — return src unchanged so we never
    introduce a regression by misclassifying.
    """
    if not src:
        return src
    if lang not in ("javascript", "typescript", "tsx", "python",
                     "c", "cpp", "go", "rust", "java", "csharp",
                     "kotlin", "swift", "scala", "ruby"):
        return src
    out = list(src)
    i, n = 0, len(src)
    # Comment + string syntax per language family.
    line_comment_token = "//"
    block_open_token = "/*"
    block_close_token = "*/"
    string_quotes = ("'", '"')
    has_template = lang in ("javascript", "typescript", "tsx")
    has_triple_quote = lang == "python"
    if lang == "python":
        line_comment_token = "#"
        block_open_token = ""   # Python has no block comments
    elif lang == "ruby":
        line_comment_token = "#"
        block_open_token = ""   # Ruby block-comment is =begin/=end, ignore
    while i < n:
        c = src[i]
        # Line comment
        if line_comment_token and src.startswith(line_comment_token, i):
            j = src.find("\n", i)
            if j == -1:
                j = n
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        # Block comment
        if block_open_token and src.startswith(block_open_token, i):
            j = src.find(block_close_token, i + len(block_open_token))
            if j == -1:
                j = n
            else:
                j += len(block_close_token)
            for k in range(i, j):
                out[k] = " " if src[k] != "\n" else "\n"
            i = j
            continue
        # Triple-quoted (Python). Check before single quote so it wins.
        # Always scan past so `//` or other tokens INSIDE the string
        # aren't misclassified; only mask the contents when requested.
        if has_triple_quote and (
                src.startswith("\"\"\"", i) or
                src.startswith("\'\'\'", i)):
            quote = src[i:i+3]
            j = src.find(quote, i + 3)
            if j == -1:
                j = n
            else:
                j += 3
            if mask_strings:
                for k in range(i, j):
                    out[k] = " " if src[k] != "\n" else "\n"
            i = j
            continue
        # JS/TS template literal (backtick) — no `${...}` interpolation
        # awareness, just skip the whole literal as a span.
        if has_template and c == "`":
            j = i + 1
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    j += 2; continue
                if src[j] == "`":
                    j += 1; break
                j += 1
            if mask_strings:
                for k in range(i, j):
                    out[k] = " " if src[k] != "\n" else "\n"
            i = j
            continue
        # Single / double quoted string — same dual-mode logic.
        if c in string_quotes:
            quote = c
            j = i + 1
            while j < n:
                if src[j] == "\\" and j + 1 < n:
                    j += 2; continue
                if src[j] == quote:
                    j += 1; break
                if src[j] == "\n":
                    # Unterminated string — bail at the line break.
                    break
                j += 1
            if mask_strings:
                for k in range(i, j):
                    out[k] = " " if src[k] != "\n" else "\n"
            i = j
            continue
        i += 1
    return "".join(out)


def scan_regex_contracts(store: Store, rel_path: str, src: str, lang: str) -> None:
    # P0 (audit) — comment masking only. Two source views:
    #
    #   `code_no_comments`: comments stripped, strings VISIBLE. Used by
    #     extractors that match a function call whose argument is a
    #     string — `getenv("X")`, `os.Getenv("X")`, `process.env["X"]`,
    #     `SafeGetenv("X")`, etc. The function-call boundary itself
    #     anchors these patterns; if the call appears inside a comment
    #     the whole match is masked out. No need to also kill strings.
    #
    #   `code_no_strings`: comments AND strings stripped. Used by
    #     extractors that match a BARE identifier — `process.env.X`,
    #     `--flag`, `--feature-name`. Without string masking, a string
    #     literal like `'process.env.DEBUG is how we debug'` gets
    #     indexed as a real env read; with masking, it's correctly
    #     ignored.
    #
    # P1#4 — keep the ORIGINAL src for `_scan_tokens` at the bottom,
    # which legitimately reads string contents (Prisma enum values,
    # config tokens like `'WEBHOOK_FAILED'`).
    original_src = src
    code_no_comments = _mask_comments_and_strings(src, lang, mask_strings=False)
    code_no_strings  = _mask_comments_and_strings(src, lang, mask_strings=True)

    # FLAG_RX matches `--bare-flag` — bare identifiers; needs strings masked
    # so flags mentioned in error-message strings or fixtures don't count.
    for m in FLAG_RX.finditer(code_no_strings):
        store.add_contract(kind="flag", name=m.group(1), file=rel_path,
                           line=code_no_strings.count("\n", 0, m.start()) + 1,
                           role="occurrence", confidence="low",
                           context="token")
    # Env extractors split into two groups by where the env name lives:
    env_rxs_in_property = [          # name follows `process.env.` (no string)
        (JS_ENV_RX, "process.env"),
    ]
    env_rxs_in_string = [            # name lives inside a string argument
        (JS_ENV_RX2, "process.env[]"),
        (GO_ENV_RX, "os.Getenv"),
        (RUST_ENV_RX, "env::var"),
        (C_ENV_RX, "getenv"),
        (JAVA_ENV_RX, "System.getenv"),
        (RUBY_ENV_RX, "ENV[]"),
    ]
    # Extra C/C++ wrappers — only enabled for C/C++ to keep the broader
    # ``->Get("X")`` style patterns from firing on JS / Go. Discovered
    # missing on nodejs/node v22.11.0 (see docs/REAL_STRESS_FINAL.md §5).
    # All of these match `func("X")` form → name is in a string → use
    # the comments-only mask (strings stay visible).
    if lang in ("c", "cpp"):
        env_rxs_in_string.extend([
            (C_ENV_SAFEGETENV_RX, "SafeGetenv"),
            (C_ENV_VARS_GET_RX, "env_vars->Get"),
            (C_ENVIRONMENT_GET_RX, "environment->Get"),
            (C_WIN32_ENV_RX, "GetEnvironmentVariable"),
        ])
    # Convention prefixes: env names with certain prefixes signal client-
    # side exposure / framework conventions. We tag them so that claim
    # verification and contract analysis can treat "NEXT_PUBLIC_X was
    # added" as "client-side env surface changed" without flagging every
    # server-side env too. Same shape as regular env contracts; only the
    # `context` field changes.
    _PREFIX_CONVENTIONS = (
        ("NEXT_PUBLIC_", "next-public"),
        ("VITE_",        "vite-public"),
        ("REACT_APP_",   "react-app"),
        ("EXPO_PUBLIC_", "expo-public"),
        ("PUBLIC_",      "public"),   # SvelteKit convention
    )

    def _prefix_context(name: str, fallback: str) -> str:
        for prefix, tag in _PREFIX_CONVENTIONS:
            if name.startswith(prefix):
                return f"{fallback}+{tag}"
        return fallback

    # Property-form extractors: name is a bare identifier — must mask
    # strings to avoid string-embedded `process.env.X` false positives.
    for rx, ctx in env_rxs_in_property:
        for m in rx.finditer(code_no_strings):
            nm = m.group(1)
            store.add_contract(kind="env", name=nm, file=rel_path,
                               line=code_no_strings.count("\n", 0, m.start()) + 1,
                               role="read", confidence="high",
                               context=_prefix_context(nm, ctx))
    # String-form extractors: name lives INSIDE a string argument — only
    # mask comments. Comments alone are enough; the function-call
    # boundary anchors the pattern so a casual mention won't match.
    for rx, ctx in env_rxs_in_string:
        for m in rx.finditer(code_no_comments):
            nm = m.group(1)
            store.add_contract(kind="env", name=nm, file=rel_path,
                               line=code_no_comments.count("\n", 0, m.start()) + 1,
                               role="read", confidence="high",
                               context=_prefix_context(nm, ctx))

    # JS/TS computed env access: `process.env[variable]` or
    # `process.env[fn(...)]`. We DON'T know the runtime name — that's
    # the point. Emit a sentinel `__computed__` entry with role
    # 'dynamic-access' so claim verification can detect "literal env
    # claims at this file may be incomplete".
    #
    # The `(?!['"])` negative lookahead in JS_ENV_COMPUTED_RX already
    # excludes literal bracket accesses (those are JS_ENV_RX2's
    # territory), so no extra "surrounding quote" check is needed.
    if lang in ("javascript", "typescript", "tsx"):
        computed_sites: set = set()
        for rx in (JS_ENV_COMPUTED_RX, JS_ENV_COMPUTED_CALL_RX):
            for m in rx.finditer(src):
                line = src.count("\n", 0, m.start()) + 1
                key = (rel_path, line)
                if key in computed_sites:
                    continue
                computed_sites.add(key)
                store.add_contract(
                    kind="env", name="__computed__",
                    file=rel_path, line=line,
                    role="dynamic-access", confidence="medium",
                    context="process.env[expr]")

        # Schema-library declarations. Record the declared env names so
        # a project using Zod / @t3-oss/env-core / envalid has its env
        # contract surface visible even if no literal `process.env.FOO`
        # read appears in the source (common with t3-env — the schema
        # generates a typed proxy and consumers read `env.FOO`).
        for schema_rx, schema_ctx in (
                (_ZOD_ENV_SCHEMA_BLOCK, "zod-schema"),
                (_T3_ENV_BLOCK,          "t3-env-schema"),
                (_ENVALID_BLOCK,         "envalid-schema")):
            for sblock in schema_rx.finditer(src):
                body = sblock.group(1)
                block_start = sblock.start(1)
                for fm in _SCHEMA_FIELD_RX.finditer(body):
                    name = fm.group(1)
                    line = src.count(
                        "\n", 0, block_start + fm.start()) + 1
                    store.add_contract(
                        kind="env", name=name, file=rel_path, line=line,
                        role="declare", confidence="high",
                        context=schema_ctx)

    # Java Bean setters → flag writes. Convert `setAllowBackslash(true)`
    # into a flag contract named `allowBackslash`. The setter call site
    # is the WRITE; the field name (lowercased first char) is the flag
    # subject. Lets `projmem flow allowBackslash --kind flag` enumerate
    # every site that flips the property — directly addresses the
    # Tomcat blind spot (#8).
    #
    # Filter out method DECLARATIONS (`public void setFoo(boolean v)`) —
    # they match the same regex but aren't writes. Heuristic: the word
    # immediately before `set...` is a Java modifier / return type
    # keyword. False negatives here just miss declarations, which we
    # don't want to capture anyway.
    if lang == "java":
        _DECL_PRECEDERS = {
            "void", "boolean", "int", "long", "short", "byte",
            "float", "double", "char", "String", "Object", "Class",
            "public", "private", "protected", "static", "final",
            "abstract", "synchronized", "native", "default",
        }
        for m in JAVA_BEAN_SETTER_RX.finditer(code_no_strings):
            cap = m.group(1)
            if not cap:
                continue
            # Reject declarations: look at the word immediately before
            # the matched `set...`. If it's a modifier / return type,
            # skip. Bare-call matches (inside method bodies) typically
            # have `;`, `{`, `}`, or whitespace after a statement sep.
            ms = m.start()
            # Walk backward over whitespace, then capture preceding word.
            j = ms
            while j > 0 and code_no_strings[j - 1] in " \t":
                j -= 1
            word_end = j
            while j > 0 and code_no_strings[j - 1].isalnum():
                j -= 1
            preceder = code_no_strings[j:word_end]
            if preceder in _DECL_PRECEDERS:
                continue
            # Also reject when the arg looks like a type+name pair
            # (`boolean v`, `String name`) — another declaration tell.
            arg = (m.group(2) or "").strip()
            if re.match(r"^(?:[A-Za-z_][\w<>\[\].]*)\s+[A-Za-z_]\w*\s*$", arg):
                continue
            # PascalCase → camelCase. setAllowBackslash → allowBackslash.
            flag_name = cap[0].lower() + cap[1:]
            ctx_kind = "java-setter"
            if arg in ("true", "false"):
                ctx_kind = f"java-setter:{arg}"
            store.add_contract(
                kind="flag", name=flag_name, file=rel_path,
                line=code_no_strings.count("\n", 0, m.start()) + 1,
                role="write", confidence="high",
                context=ctx_kind)
    # Spring / Tomcat XML config — `<property name="allowBackslash">`
    # references the same Bean setter through a different surface. Flag
    # named identically; context=xml-property so consumers can split.
    if rel_path.endswith(".xml"):
        for m in JAVA_XML_SETTER_RX.finditer(original_src):
            store.add_contract(
                kind="flag", name=m.group(1), file=rel_path,
                line=original_src.count("\n", 0, m.start()) + 1,
                role="declare", confidence="medium",
                context="xml-property")

    # Go struct tags → schema field writes (serialization boundary).
    if lang == "go":
        for m in GO_STRUCT_TAG_RX.finditer(src):
            field = m.group(1)
            if field and field != "-":
                store.add_contract(kind="schema_field", name=field, file=rel_path,
                                   line=src.count("\n", 0, m.start()) + 1,
                                   role="write", confidence="high",
                                   context="go-struct-tag")
    # Rust serde rename → same idea.
    if lang == "rust":
        for m in RUST_SERDE_RX.finditer(src):
            store.add_contract(kind="schema_field", name=m.group(1), file=rel_path,
                               line=src.count("\n", 0, m.start()) + 1,
                               role="write", confidence="high",
                               context="rust-serde-rename")
    for m in COMMANDER_OPT_RX.finditer(src):
        store.add_contract(kind="flag", name=m.group(1), file=rel_path,
                           line=src.count("\n", 0, m.start()) + 1,
                           role="parse", confidence="high", context="commander")
    # Dotted-assignment writes: `obj.status = 'clean'`, `this.state.foo = ...`.
    # Was silently missed before; was a dominant write pattern in pwnpilot's
    # scanner.js and controller/. Attr-assign is stronger signal than an
    # object-literal key, so we mark it `confidence=medium` and role=write_assign.
    if lang in ("javascript", "typescript"):
        for m in JS_ATTR_ASSIGN_RX.finditer(src):
            k = m.group(1)
            if not _looks_like_field(k):
                continue
            # Skip if on a `case`-like or inside a comment — crude but useful.
            line_start = src.rfind("\n", 0, m.start()) + 1
            preceding = src[line_start:m.start()]
            if "//" in preceding or re.match(r"^\s*\*", preceding):
                continue
            store.add_contract(
                kind="schema_field", name=k, file=rel_path,
                line=src.count("\n", 0, m.start()) + 1,
                role="write_assign", confidence="medium",
                context="attr-assign")

    for m in JS_OBJ_KEY_RX.finditer(src):
        k = m.group(1)
        if not _looks_like_field(k):
            continue
        # Skip `case "X":` switch labels — same syntax, different semantics.
        line_start = src.rfind("\n", 0, m.start()) + 1
        preceding = src[line_start:m.start()]
        if re.search(r"\bcase\s*$", preceding):
            continue
        store.add_contract(kind="schema_field", name=k, file=rel_path,
                           line=src.count("\n", 0, m.start()) + 1,
                           role="write", confidence="low", context="obj-key")
    for m in JS_INDEX_KEY_RX.finditer(src):
        k = m.group(1)
        if _looks_like_field(k):
            store.add_contract(kind="schema_field", name=k, file=rel_path,
                               line=src.count("\n", 0, m.start()) + 1,
                               role="read", confidence="low", context="index-access")

    # Event / listener pairs — JS/TS idioms. Captures the EVENT NAME string
    # in EventEmitter / DOM / CDP-style APIs so consumers can ask:
    #   "who listens for 'Page.loadEventFired'?"  (scanner.js ↔ controller)
    #   "who emits 'shouldStop'?"                  (scheduler ↔ worker)
    # Case-insensitive language check because some callers pass 'typescript'.
    if lang in ("javascript", "typescript"):
        for m in EV_LISTEN_RX.finditer(src):
            store.add_contract(
                kind="event", name=m.group(1), file=rel_path,
                line=src.count("\n", 0, m.start()) + 1,
                role="listen", confidence="medium", context="event-on")
        for m in EV_EMIT_RX.finditer(src):
            # Avoid double-capture when the emitter is actually the
            # `dispatchEvent(new Event("X"))` shape handled separately.
            ctx = src[max(0, m.start() - 14):m.end() + 30]
            if "dispatchEvent" in ctx and "new " in ctx:
                continue
            store.add_contract(
                kind="event", name=m.group(1), file=rel_path,
                line=src.count("\n", 0, m.start()) + 1,
                role="emit", confidence="medium", context="event-emit")
        for m in EV_DISPATCH_NEW_RX.finditer(src):
            store.add_contract(
                kind="event", name=m.group(1), file=rel_path,
                line=src.count("\n", 0, m.start()) + 1,
                role="emit", confidence="medium", context="dispatchEvent(new)")

    # _scan_tokens reads string-literal contents (the legitimate place
    # where enum values like 'WEBHOOK_FAILED' live), so pass the
    # ORIGINAL source — not the env/flag-masked one.
    _scan_tokens(store, rel_path, original_src)

    # Enum-shape extraction. Captures (enum_name, [members]) from TS, Prisma
    # DSL, SQL CREATE TYPE, and Rust enums, stores them as kind='enum_shape'
    # contracts so the cross-layer check in checklist.py can flag the case
    # where a TS enum and a Prisma enum with the same name have drifted
    # apart on member set. Audit reproduced this on a Next.js+Prisma
    # codebase with a `NotificationType` enum mismatch — `complete` said
    # "ok" because (kind, name) matching never crossed language layers.
    _scan_enum_shapes(store, rel_path, code_no_comments)


_STOP_TOKEN_WORDS = {
    "true", "false", "null", "none", "undefined", "utf-8", "utf8",
}


def _looks_like_field(k: str) -> bool:
    if not k or len(k) < 2 or len(k) > 40:
        return False
    if k in _STOP_TOKEN_WORDS:
        return False
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_\-]*$", k):
        return False
    return True


_ENUM_BLOCK_RX = re.compile(
    r"\benum\s+([A-Za-z_][\w]*)\s*\{([^}]*)\}",
    re.MULTILINE,
)
_SQL_ENUM_RX = re.compile(
    r"CREATE\s+TYPE\s+([A-Za-z_][\w]*)\s+AS\s+ENUM\s*\(([^)]*)\)",
    re.IGNORECASE,
)
# Modern TypeScript pattern: `export const Xs = ["A","B","C"] as const;`
# Used instead of `enum X { ... }` to avoid runtime enum objects. Common
# in Next.js, zod, drizzle, tRPC codebases. Must be captured for the
# cross-layer enum-drift check to fire on real projects.
_TS_AS_CONST_ARRAY_RX = re.compile(
    r"""(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*"""
    r"""(?::\s*(?:readonly\s+)?\[[^\]]*\])?"""  # optional type annotation
    r"""\s*=\s*\[([^\]]*)\]\s*as\s+const""",
    re.MULTILINE | re.DOTALL,
)
_TS_ENUM_MEMBER_RX = re.compile(r"^\s*([A-Za-z_$][\w$]*)")
_PRISMA_MEMBER_RX = re.compile(r"^[A-Za-z_][\w]*$")
_AS_CONST_STRING_RX = re.compile(r"['\"]([A-Za-z_][\w]*)['\"]")


def _ts_enum_members(body: str) -> List[str]:
    """Members from a TS enum body. Comma-separated; each may have an
    `= 'value'` initializer which we strip."""
    out: List[str] = []
    for raw in body.split(","):
        m = _TS_ENUM_MEMBER_RX.match(raw)
        if m:
            out.append(m.group(1))
    return out


def _prisma_enum_members(body: str) -> List[str]:
    """Members from a Prisma enum body. Whitespace/newline separated; no
    commas, no equals. Skip `@@map(...)` attribute lines."""
    out: List[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("@@") or line.startswith("//"):
            continue
        for tok in line.split():
            if tok.startswith("@"):
                break
            if _PRISMA_MEMBER_RX.match(tok):
                out.append(tok)
    return out


def _sql_enum_members(body: str) -> List[str]:
    """Members from a SQL `CREATE TYPE ... AS ENUM ('A', 'B')` body."""
    out: List[str] = []
    for m in re.finditer(r"['\"]([A-Za-z_][\w]*)['\"]", body):
        out.append(m.group(1))
    return out


def _rust_enum_members(body: str) -> List[str]:
    """Variant names from a Rust enum body. Comma-separated at top level;
    nested struct-variant bodies (`{...}`) are cut off by the outer
    `[^}]*` capture but the variant NAME comes first so we still get it."""
    out: List[str] = []
    for raw in re.split(r",\s*", body):
        m = re.match(r"\s*([A-Z][\w]*)", raw)
        if m:
            out.append(m.group(1))
    return out


def _scan_enum_shapes(store: Store, rel_path: str, code_no_comments: str) -> None:
    """Capture enum declarations as `kind='enum_shape'` contracts. The
    member set is stored in `context` as a sorted, comma-joined string so
    cross-layer reconciliation can compare set membership cheaply.

    Lang dispatch by file extension (not the parser `lang` arg) since
    Prisma/SQL files don't have a tree-sitter parser registered but DO
    have predictable enum syntax we can capture by regex."""
    import os as _os
    ext = _os.path.splitext(rel_path)[1].lower()

    if ext == ".prisma":
        for m in _ENUM_BLOCK_RX.finditer(code_no_comments):
            name = m.group(1)
            members = _prisma_enum_members(m.group(2))
            if not members:
                continue
            store.add_contract(
                kind="enum_shape", name=name, file=rel_path,
                line=code_no_comments.count("\n", 0, m.start()) + 1,
                role="declare", confidence="high",
                context="prisma:" + ",".join(sorted(set(members))))
        return

    if ext == ".sql":
        for m in _SQL_ENUM_RX.finditer(code_no_comments):
            name = m.group(1)
            members = _sql_enum_members(m.group(2))
            if not members:
                continue
            store.add_contract(
                kind="enum_shape", name=name, file=rel_path,
                line=code_no_comments.count("\n", 0, m.start()) + 1,
                role="declare", confidence="high",
                context="sql:" + ",".join(sorted(set(members))))
        return

    if ext in (".ts", ".tsx"):
        # Classic `enum X { ... }` form.
        for m in _ENUM_BLOCK_RX.finditer(code_no_comments):
            name = m.group(1)
            members = _ts_enum_members(m.group(2))
            if not members:
                continue
            store.add_contract(
                kind="enum_shape", name=name, file=rel_path,
                line=code_no_comments.count("\n", 0, m.start()) + 1,
                role="declare", confidence="high",
                context="ts:" + ",".join(sorted(set(members))))
        # Modern `export const Xs = ["A","B"] as const;` form. Emits
        # ts:... just like the enum form so the cross-layer check in
        # checklist.py reconciles them against Prisma / SQL enums.
        # Audit: OpsCanvas used this pattern for all shared enums;
        # without this the gate never fired.
        for m in _TS_AS_CONST_ARRAY_RX.finditer(code_no_comments):
            name = m.group(1)
            body = m.group(2)
            members = [mm.group(1)
                       for mm in _AS_CONST_STRING_RX.finditer(body)]
            if not members:
                continue
            # Strip trailing pluralisation so `NotificationTypes` is
            # reconciled against Prisma's `NotificationType`. Standard
            # convention in TS: the const array is the plural; the type
            # alias is the singular. We index under BOTH so ambiguous
            # cases (Roles vs Role in Prisma both exist) still match.
            canonical = name[:-1] if name.endswith("s") and len(name) > 1 else name
            for emit_name in {name, canonical}:
                store.add_contract(
                    kind="enum_shape", name=emit_name, file=rel_path,
                    line=code_no_comments.count("\n", 0, m.start()) + 1,
                    role="declare", confidence="high",
                    context="ts:" + ",".join(sorted(set(members))))
        return

    if ext == ".rs":
        for m in _ENUM_BLOCK_RX.finditer(code_no_comments):
            name = m.group(1)
            members = _rust_enum_members(m.group(2))
            if not members:
                continue
            store.add_contract(
                kind="enum_shape", name=name, file=rel_path,
                line=code_no_comments.count("\n", 0, m.start()) + 1,
                role="declare", confidence="high",
                context="rust:" + ",".join(sorted(set(members))))


def _scan_tokens(store: Store, rel_path: str, src: str) -> None:
    """Record occurrences of short UPPERCASE-style or repeated identifier-like literals
    so that later cross-file aggregation can flag real tokens. Keep volume bounded."""
    seen = set()
    for m in STRING_LITERAL_RX.finditer(src):
        v = m.group(1) or m.group(2)
        if not v:
            continue
        if v.lower() in _STOP_TOKEN_WORDS:
            continue
        # Candidate: UPPER_SNAKE, or kebab-case const-like, or CamelCase enum-like
        if re.match(r"^[A-Z][A-Z0-9_]{2,30}$", v) or re.match(r"^[a-z]+(?:[-_][a-z]+){1,4}$", v):
            key = (v,)
            if key in seen:
                continue
            seen.add(key)
            store.add_contract(kind="token", name=v, file=rel_path,
                               line=src.count("\n", 0, m.start()) + 1,
                               role="occurrence", confidence="low",
                               context="string-literal")


def scan_package_json(store: Store, rel_path: str, src: str) -> None:
    """Round-X report: `package.json` is a contract surface — parse it into
    structured contract entities so downstream queries (`projmem contracts
    --kind script`, drift checks) can reason about it. Emits:
      kind=entrypoint, name='main'/'module'/'bin:<n>'   role=declare
      kind=script,     name='<script-name>'              role=declare
      kind=dep,        name='<package>'                  role=declare
    Confidence high — these are exact JSON parses, not heuristics.
    """
    import json as _json
    try:
        data = _json.loads(src)
    except _json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return

    def _add(kind: str, name: str, value: str, role: str = "declare") -> None:
        store.add_contract(
            kind=kind, name=str(name), file=rel_path, line=0,
            role=role, confidence="high",
            context=f"package.json:{kind}={value}")

    for k in ("main", "module"):
        v = data.get(k)
        if isinstance(v, str):
            _add("entrypoint", k, v)
    bins = data.get("bin")
    if isinstance(bins, str):
        _add("entrypoint", "bin", bins)
    elif isinstance(bins, dict):
        for bn, bv in bins.items():
            if isinstance(bv, str):
                _add("entrypoint", f"bin:{bn}", bv)
    scripts = data.get("scripts") or {}
    if isinstance(scripts, dict):
        for sn, sv in scripts.items():
            if isinstance(sv, str):
                _add("script", sn, sv)
    for dep_field in ("dependencies", "devDependencies", "peerDependencies",
                      "optionalDependencies"):
        deps = data.get(dep_field) or {}
        if isinstance(deps, dict):
            for dn, dv in deps.items():
                _add("dep", dn, str(dv) if dv is not None else "",
                     role=dep_field)


def apply_user_contracts(store: Store, cfg: Config) -> None:
    c = cfg.contracts or {}
    for name in c.get("flags", []) or []:
        store.add_contract(kind="flag", name=str(name).lstrip("-"), file="<config>",
                           line=0, role="declare", confidence="high",
                           context="user-config")
    for name in c.get("env", []) or []:
        store.add_contract(kind="env", name=str(name), file="<config>",
                           line=0, role="declare", confidence="high",
                           context="user-config")
    for name in c.get("schema_fields", []) or []:
        store.add_contract(kind="schema_field", name=str(name), file="<config>",
                           line=0, role="declare", confidence="high",
                           context="user-config")
    for name in c.get("tokens", []) or []:
        store.add_contract(kind="token", name=str(name), file="<config>",
                           line=0, role="declare", confidence="high",
                           context="user-config")
    for pair in c.get("pairs", []) or []:
        if not isinstance(pair, dict):
            continue
        src = pair.get("if_touch")
        for tgt in pair.get("inspect", []) or []:
            if src and tgt:
                store.add_edge(src=src, dst=tgt, type="pair_inspect",
                               confidence="high",
                               evidence="user-config: if_touch/inspect")
