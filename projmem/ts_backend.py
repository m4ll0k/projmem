"""Tree-sitter indexing backend. Optional — degrades gracefully if unavailable.

Design:
  - One dispatch entry point: `index(store, rel_path, src, lang_key, root) -> bool`.
  - Returns True on success (caller should skip the regex fallback).
  - Queries are minimal and defensive: failures in one language never break others.
  - Resolves imports to files where we can; records `module:<spec>` for externals.
"""
from __future__ import annotations
import os
import re
from typing import Dict, List, Optional, Tuple

# ---- availability ----------------------------------------------------------
try:
    from tree_sitter import Query, QueryCursor
    from tree_sitter_language_pack import get_language, get_parser
    _AVAILABLE = True
except Exception:  # pragma: no cover
    _AVAILABLE = False


def available() -> bool:
    return _AVAILABLE


# Map our internal language label / file extension → tree-sitter grammar name.
EXT_TO_TS: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".java": "java",
    ".rb": "ruby",
    ".cs": "csharp",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift",
    ".php": "php",
    ".scala": "scala",
    ".sh": "bash", ".bash": "bash",
}


_CPP_HINT_RX = None  # lazy-compiled — see ts_lang_for_with_content


def ts_lang_for(path: str) -> Optional[str]:
    return EXT_TO_TS.get(os.path.splitext(path)[1].lower())


def ts_lang_for_with_content(path: str, src: str) -> Optional[str]:
    """Like ``ts_lang_for`` but consults file content to disambiguate
    `.h` headers — most modern Node.js / LLVM / V8 / etc. headers are
    C++ even though the extension is `.h`. The C parser cannot handle
    `class`, `namespace`, or qualified types, so misrouting causes
    silent symbol loss (discovered on nodejs/node v22.11.0).
    """
    base = ts_lang_for(path)
    if base != "c":
        return base
    global _CPP_HINT_RX
    if _CPP_HINT_RX is None:
        import re as _re
        # Conservative C++-only tokens. Any one match flips to cpp.
        _CPP_HINT_RX = _re.compile(
            r"(?m)^\s*(?:namespace\s+\w|class\s+\w|template\s*<|"
            r"using\s+namespace|public\s*:|private\s*:|protected\s*:)"
            r"|::\w|std::")
    return "cpp" if (src and _CPP_HINT_RX.search(src)) else "c"


# ---- queries ---------------------------------------------------------------
# Capture-name convention:
#   sym.<kind>    — a definition (function, class, method, type, struct, etc.)
#   import.path   — string specifier (JS/TS/Go/Python relative/C include/Rust)
#   import.module — dotted module path (Python/Java)
#   ref.call      — call site function/method identifier
QUERIES: Dict[str, str] = {
    "python": r"""
        (function_definition name: (identifier) @sym.function)
        (class_definition    name: (identifier) @sym.class)
        (import_statement (dotted_name) @import.module)
        (import_from_statement module_name: (dotted_name) @import.module)
        (import_from_statement module_name: (relative_import) @import.module)
        (import_from_statement) @py_from_stmt
        (call function: (identifier) @ref.call)
        (call function: (attribute attribute: (identifier) @ref.call))

        ; Attribute-access receiver — `mod` in `mod.hello()` or `obj` in
        ; `obj.field`. Matches the AST path's `visit_Attribute` (captures
        ; leftmost Name), without which a `from . import mod; mod.x()`
        ; usage would leave zero refs to `mod` in the index.
        (attribute object: (identifier) @ref.name)

        ; M3: Python inheritance. `class Dog(Animal):` — emit extends edge.
        (class_definition
            name: (identifier) @_subclass
            superclasses: (argument_list (identifier) @rel.extends))
    """,
    "javascript": r"""
        (function_declaration name: (identifier) @sym.function)
        (class_declaration    name: (identifier) @sym.class)
        (method_definition    name: (property_identifier) @sym.method)

        ; Top-level variable declarations (covers consts, IIFEs, module-scoped state)
        (program (lexical_declaration
            (variable_declarator name: (identifier) @sym.var)))
        (program (variable_declaration
            (variable_declarator name: (identifier) @sym.var)))

        ; `export const handler = ...` — the wrapper hides the const
        ; from the program-level pattern. Mirror of TS query.
        (export_statement (lexical_declaration
            (variable_declarator name: (identifier) @sym.var)))
        (export_statement (variable_declaration
            (variable_declarator name: (identifier) @sym.var)))

        ; const X = class {...}  — class expression assigned to a name
        (variable_declarator
            name: (identifier) @sym.class
            value: (class))

        ; module.exports.NAME = ...
        (assignment_expression
            left: (member_expression
                object: (member_expression
                    object: (identifier) @_m
                    property: (property_identifier) @_e)
                property: (property_identifier) @sym.exported)
            (#eq? @_m "module") (#eq? @_e "exports"))

        ; exports.NAME = ...
        (assignment_expression
            left: (member_expression
                object: (identifier) @_e2
                property: (property_identifier) @sym.exported)
            (#eq? @_e2 "exports"))

        ; Foo.prototype.method = fn  (legacy OO)
        (assignment_expression
            left: (member_expression
                object: (member_expression
                    property: (property_identifier) @_proto)
                property: (property_identifier) @sym.method)
            (#eq? @_proto "prototype"))

        ; Imports / exports-from. Barrel re-exports (`export * from "..."`)
        ; are tagged distinctly so reverse_deps can follow them transitively
        ; — without this, any symbol exposed only via a compiler namespace
        ; barrel (TypeScript's src/compiler/_namespaces/ts.ts pattern) gets
        ; zero reverse dependencies.
        (import_statement source: (string (string_fragment) @import.path))
        (export_statement
            (export_clause)
            source: (string (string_fragment) @import.path.reexport_named))
        (export_statement
            "*"
            source: (string (string_fragment) @import.path.reexport_star))
        (export_statement source: (string (string_fragment) @import.path))
        (call_expression function: (identifier) @_req
                         arguments: (arguments (string (string_fragment) @import.path))
                         (#eq? @_req "require"))

        ; Call-site references
        (call_expression function: (identifier) @ref.call)
        (call_expression function: (member_expression property: (property_identifier) @ref.call))

        ; Member-expression RECEIVER — `obj` in `obj.method()` or `obj.field`.
        ; Without this capture, an imported module used dottedly never lands
        ; in refs, which breaks cross-file `projmem symbol <module>` lookups.
        (member_expression object: (identifier) @ref.name)

        ; Node.js `internalBinding('X')` — capture the string arg as
        ; an import edge to `src/node_X.cc` (mapped at resolve time).
        ; Without this, the C++/JS boundary in Node-style codebases
        ; (Node, Deno, Electron, Bun) stays invisible to projmem.
        (call_expression
            function: (identifier) @_ib_id
            arguments: (arguments
                (string (string_fragment) @import.path.binding))
            (#match? @_ib_id "^(internalBinding|InternalBinding)$"))

        ; Round-3 report bug #4: widen JS ref coverage beyond call-sites.
        ; `new Foo(...)` — constructor usage = real reference.
        (new_expression constructor: (identifier) @ref.new)
        ; Destructured CommonJS require: `const { X, Y } = require('...')`
        (variable_declarator
            name: (object_pattern (shorthand_property_identifier_pattern) @ref.import_binding)
            value: (call_expression function: (identifier) @_req3)
            (#eq? @_req3 "require"))
        ; Destructured ESM import: `import { X, Y } from '...';`
        (import_specifier name: (identifier) @ref.import_binding)

        ; Round-4 report P1: callback-passing (`.map(normalizeInputUrlLine)`)
        ; — an identifier used as a function-position argument IS a real
        ; reference to that function. Without this capture, `orphans`
        ; flagged legitimate callbacks as dead.
        (call_expression arguments: (arguments (identifier) @ref.callback))
        ; Shorthand object property — used both in `{name}` destructuring
        ; exports (`module.exports = { normalizeInputUrlLine }`) and in any
        ; object literal using the same name as the binding. The shorthand
        ; form is BY DEFINITION a reference to the binding of the same name.
        (shorthand_property_identifier) @ref.shorthand

        ; M3: inheritance. `class Dog extends Animal {}` — Animal is a ref
        ; AND a parent of Dog. Capture the parent identifier so we can emit
        ; an `extends` edge at the indexer layer.
        (class_declaration
            name: (identifier) @_subclass
            (class_heritage (identifier) @rel.extends))
    """,
    "typescript": r"""
        (function_declaration name: (identifier) @sym.function)
        (class_declaration    name: (type_identifier) @sym.class)
        (interface_declaration name: (type_identifier) @sym.interface)
        (type_alias_declaration name: (type_identifier) @sym.type)
        (enum_declaration     name: (identifier) @sym.enum)
        (method_definition    name: (property_identifier) @sym.method)

        (program (lexical_declaration
            (variable_declarator name: (identifier) @sym.var)))
        (program (variable_declaration
            (variable_declarator name: (identifier) @sym.var)))

        ; `export const handler = ...` / `export let x = ...` — the
        ; export wrapper means the lexical_declaration is no longer
        ; a direct child of `program`. Capture them explicitly.
        ; Discovered missing via tests/test_parser_hardening.py.
        (export_statement (lexical_declaration
            (variable_declarator name: (identifier) @sym.var)))
        (export_statement (variable_declaration
            (variable_declarator name: (identifier) @sym.var)))

        (variable_declarator
            name: (identifier) @sym.class
            value: (class))

        (assignment_expression
            left: (member_expression
                object: (member_expression
                    object: (identifier) @_m
                    property: (property_identifier) @_e)
                property: (property_identifier) @sym.exported)
            (#eq? @_m "module") (#eq? @_e "exports"))

        (assignment_expression
            left: (member_expression
                object: (identifier) @_e2
                property: (property_identifier) @sym.exported)
            (#eq? @_e2 "exports"))

        (assignment_expression
            left: (member_expression
                object: (member_expression
                    property: (property_identifier) @_proto)
                property: (property_identifier) @sym.method)
            (#eq? @_proto "prototype"))

        (import_statement source: (string (string_fragment) @import.path))
        (export_statement
            (export_clause)
            source: (string (string_fragment) @import.path.reexport_named))
        (export_statement
            "*"
            source: (string (string_fragment) @import.path.reexport_star))
        (export_statement source: (string (string_fragment) @import.path))
        (call_expression function: (identifier) @_req
                         arguments: (arguments (string (string_fragment) @import.path))
                         (#eq? @_req "require"))

        (call_expression function: (identifier) @ref.call)
        (call_expression function: (member_expression property: (property_identifier) @ref.call))

        ; Member-expression receiver — `obj` in `obj.method()` or `obj.field`.
        (member_expression object: (identifier) @ref.name)

        (new_expression constructor: (identifier) @ref.new)
        (variable_declarator
            name: (object_pattern (shorthand_property_identifier_pattern) @ref.import_binding)
            value: (call_expression function: (identifier) @_req4)
            (#eq? @_req4 "require"))
        (import_specifier name: (identifier) @ref.import_binding)
        (call_expression arguments: (arguments (identifier) @ref.callback))
        (shorthand_property_identifier) @ref.shorthand

        ; M3: TS inheritance. `extends` and `implements` clauses.
        (class_declaration
            name: (type_identifier) @_subclass
            (class_heritage (extends_clause (identifier) @rel.extends)))
        (class_declaration
            name: (type_identifier) @_subclass2
            (class_heritage (implements_clause (type_identifier) @rel.implements)))
        (interface_declaration
            name: (type_identifier) @_subiface
            (extends_type_clause (type_identifier) @rel.extends))
    """,
    # tsx grammar accepts the same queries as typescript
    "tsx": None,  # filled in below
    "go": r"""
        (function_declaration name: (identifier) @sym.function)
        (method_declaration   name: (field_identifier) @sym.method)
        (type_spec name: (type_identifier) @sym.type)
        (import_spec path: (interpreted_string_literal) @import.path)
        (call_expression function: (identifier) @ref.call)
        (call_expression function: (selector_expression field: (field_identifier) @ref.call))

        ; Selector-expression RECEIVER — `pkg` in `pkg.Func()` or `s.Field`.
        ; Critical for cross-package ref tracking: without this, a Go file
        ; that imports `"internal/config"` and calls `config.Load()` never
        ; records a ref on `config`.
        (selector_expression operand: (identifier) @ref.name)
    """,
    "rust": r"""
        (function_item name: (identifier) @sym.function)
        (struct_item   name: (type_identifier) @sym.struct)
        (enum_item     name: (type_identifier) @sym.enum)
        (trait_item    name: (type_identifier) @sym.trait)
        (mod_item      name: (identifier) @sym.module)
        (use_declaration argument: (_) @import.module)
        (call_expression function: (identifier) @ref.call)
        (call_expression function: (field_expression field: (field_identifier) @ref.call))
        (call_expression function: (scoped_identifier name: (identifier) @ref.call))

        ; Field-expression RECEIVER — `obj` in `obj.method()` / `obj.field`.
        (field_expression value: (identifier) @ref.name)
        ; Scoped-identifier PATH — `module` in `module::func()` / `Trait::method()`.
        (scoped_identifier path: (identifier) @ref.name)
    """,
    "c": r"""
        (function_definition declarator: (function_declarator
            declarator: (identifier) @sym.function)
            (#not-match? @sym.function "^(if|else|while|for|switch|do|return|break|continue|goto|typedef|struct|union|enum|sizeof|static|extern|inline|const|volatile|register|auto|void|int|char|long|short|float|double|signed|unsigned)$"))
        (preproc_include path: (string_literal) @import.path)
        (preproc_include path: (system_lib_string) @import.path)
        (call_expression function: (identifier) @ref.call)

        ; Struct / union field declarations. Without this, every config
        ; flag and every option struct member is invisible to the
        ; symbol search — exactly the gap discovered on nodejs/node's
        ; `EnvironmentOptions` (see docs/REAL_STRESS_FINAL.md §5).
        (field_declaration declarator: (field_identifier) @sym.field)
        (field_declaration declarator: (array_declarator
            declarator: (field_identifier) @sym.field))
        (field_declaration declarator: (pointer_declarator
            declarator: (field_identifier) @sym.field))

        ; Function-pointer registrations. Plugin-style C registers its
        ; exported functions inside a struct — either via designated
        ; initializer (`{.handler = my_func}`) or direct field
        ; assignment (`t->handler = my_func`). Without these captures
        ; every registered handler looks like a dead orphan (Quake 3
        ; botlib had ~4000 such false positives).
        (initializer_pair designator: (field_designator) value: (identifier) @ref.callback)
        (assignment_expression left: (field_expression) right: (identifier) @ref.callback)
    """,
    "cpp": r"""
        (function_definition declarator: (function_declarator
            declarator: [(identifier) (field_identifier) (qualified_identifier)] @sym.function)
            (#not-match? @sym.function "^(if|else|while|for|switch|do|return|break|continue|goto|typedef|struct|union|enum|class|namespace|template|public|private|protected|virtual|static|extern|inline|const|volatile|mutable|constexpr|noexcept|explicit|friend|using|operator|new|delete|this|nullptr|true|false|void|int|char|long|short|float|double|signed|unsigned|auto)$"))
        (class_specifier  name: (type_identifier) @sym.class)
        (struct_specifier name: (type_identifier) @sym.struct)
        (preproc_include path: (string_literal) @import.path)
        (preproc_include path: (system_lib_string) @import.path)
        (call_expression function: (identifier) @ref.call)

        ; Field declarations inside class / struct bodies — captures
        ; member variables like `bool experimental_permission = false;`
        ; on EnvironmentOptions. Drives flag-state and option-propagation
        ; reasoning. Discovered missing on nodejs/node v22.11.0.
        ;
        ; The pattern matches the `field_identifier` child directly
        ; (tree-sitter-cpp does NOT label it as `declarator:` when the
        ; type is templated like `std::vector<T>`), and also nested
        ; under array/pointer declarators.
        (field_declaration (field_identifier) @sym.field)
        (field_declaration (array_declarator
            declarator: (field_identifier) @sym.field))
        (field_declaration (pointer_declarator
            declarator: (field_identifier) @sym.field))

        ; Method calls via member-access (obj.method() / obj->method()).
        ; Without this, callers like `permission()->Apply(...)` show up
        ; as zero refs to `Apply`. Captures the rightmost identifier
        ; only — the receiver is left to upstream resolution.
        (call_expression function: (field_expression
            field: (field_identifier) @ref.call))

        ; Namespaced / qualified calls — `ns::func(...)`, `A::B::func(...)`,
        ; `ClassName::static_method(...)`. Without this, Node.js-style code
        ; like `credentials::SafeGetenv(...)` registers zero refs to the
        ; unqualified `SafeGetenv` definition in node_credentials.cc. The
        ; rightmost `identifier` is what we record — the scope chain is
        ; carried by the AST and does not need to land in the refs table
        ; for basic callgraph reconstruction. We unroll up to four nesting
        ; levels (`a::b::c::d::func`) which covers every realistic C++ use.
        (call_expression function: (qualified_identifier
            name: (identifier) @ref.call))
        (call_expression function: (qualified_identifier
            name: (qualified_identifier
                name: (identifier) @ref.call)))
        (call_expression function: (qualified_identifier
            name: (qualified_identifier
                name: (qualified_identifier
                    name: (identifier) @ref.call))))
        (call_expression function: (qualified_identifier
            name: (qualified_identifier
                name: (qualified_identifier
                    name: (qualified_identifier
                        name: (identifier) @ref.call)))))

        ; Function-pointer registrations — same pattern as C.
        (initializer_pair designator: (field_designator) value: (identifier) @ref.callback)
        (assignment_expression left: (field_expression) right: (identifier) @ref.callback)
    """,
    "java": r"""
        (class_declaration     name: (identifier) @sym.class)
        (interface_declaration name: (identifier) @sym.interface)
        (method_declaration    name: (identifier) @sym.method)
        (enum_declaration      name: (identifier) @sym.enum)
        (import_declaration (_) @import.module)
        (method_invocation name: (identifier) @ref.call)
        ; Constructor refs (`new Connector(...)`). Without this every
        ; instantiation site disappears and reverse-deps undercount the
        ; class's true blast radius (Tomcat audit surfaced this).
        (object_creation_expression
            type: (type_identifier) @ref.new)
        ; Type-position refs (parameter types, field types, local var
        ; types, return types). Java compiles these to import-level
        ; deps but tree-sitter sees them as type_identifier nodes.
        ; Capturing them turns `void handle(Request r)` into a ref to
        ; Request — which is what `reverse Request.java` needs to find.
        (formal_parameter type: (type_identifier) @ref.name)
        (field_declaration type: (type_identifier) @ref.name)
        (local_variable_declaration type: (type_identifier) @ref.name)
        (method_declaration type: (type_identifier) @ref.name)
        ; Inheritance — extends / implements. Recorded so reverse-deps
        ; see "X extends Connector" as a Connector consumer.
        (superclass (type_identifier) @ref.name)
        (super_interfaces
            (type_list (type_identifier) @ref.name))
    """,
    "ruby": r"""
        (method name: (identifier) @sym.function)
        (class  name: (constant)  @sym.class)
        (module name: (constant)  @sym.module)
        (call   method: (identifier) @_req
                arguments: (argument_list (string (string_content) @import.path))
                (#match? @_req "^(require|require_relative|load)$"))
        (call method: (identifier) @ref.call)
    """,
    "csharp": r"""
        (class_declaration     name: (identifier) @sym.class)
        (interface_declaration name: (identifier) @sym.interface)
        (struct_declaration    name: (identifier) @sym.struct)
        (method_declaration    name: (identifier) @sym.method)
        (enum_declaration      name: (identifier) @sym.enum)
        (using_directive (_) @import.module)
        (invocation_expression function: (identifier) @ref.call)
    """,
    "kotlin": r"""
        (function_declaration (simple_identifier) @sym.function)
        (class_declaration (type_identifier) @sym.class)
        (import_header (identifier) @import.module)
    """,
    "swift": r"""
        (function_declaration name: (simple_identifier) @sym.function)
        (class_declaration name: (type_identifier) @sym.class)
        (import_declaration (identifier) @import.module)
    """,
    "php": r"""
        (function_definition name: (name) @sym.function)
        (class_declaration   name: (name) @sym.class)
        (method_declaration  name: (name) @sym.method)
    """,
    "scala": r"""
        (function_definition name: (identifier) @sym.function)
        (class_definition    name: (identifier) @sym.class)
        (object_definition   name: (identifier) @sym.object)
        (trait_definition    name: (identifier) @sym.trait)
    """,
    "bash": r"""
        (function_definition name: (word) @sym.function)
    """,
}
QUERIES["tsx"] = QUERIES["typescript"]

# Cached compiled (language, query) pairs.
_COMPILED: Dict[str, Tuple[object, object]] = {}


def _compile(ts_lang: str):
    if ts_lang in _COMPILED:
        return _COMPILED[ts_lang]
    src = QUERIES.get(ts_lang)
    if not src:
        _COMPILED[ts_lang] = (None, None)
        return _COMPILED[ts_lang]
    try:
        lang = get_language(ts_lang)
        parser = get_parser(ts_lang)
        query = Query(lang, src)
        _COMPILED[ts_lang] = (parser, query)
    except Exception:
        _COMPILED[ts_lang] = (None, None)
    return _COMPILED[ts_lang]


# ---- import resolution (per language family) -------------------------------

def _resolve_python(spec: str, rel_path: str, root: str) -> Optional[str]:
    from .indexer import resolve_python_import
    return resolve_python_import(spec, rel_path, root)


NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs",
    "http", "http2", "https", "inspector", "module", "net", "os", "path",
    "perf_hooks", "process", "punycode", "querystring", "readline", "repl",
    "stream", "string_decoder", "sys", "timers", "tls", "trace_events", "tty",
    "url", "util", "v8", "vm", "wasi", "worker_threads", "zlib",
    # with node: prefix
}


def _is_node_builtin(spec: str) -> bool:
    s = spec.strip().strip('"').strip("'")
    if s.startswith("node:"):
        return True
    # Submodules like "fs/promises" are also builtins.
    base = s.split("/", 1)[0]
    return base in NODE_BUILTINS


def _resolve_js(spec: str, rel_path: str, root: str) -> Optional[str]:
    from .indexer import resolve_js_import
    return resolve_js_import(spec, rel_path, root)


# Binding-registration macro scanner. Node registers a JS-facing binding via
# one of:
#   NODE_BINDING_CONTEXT_AWARE_INTERNAL(name, InitFn)
#   NODE_BINDING_PER_ISOLATE_INIT(name, InitFn)
#   NODE_BINDING_EXTERNAL_REFERENCE(name, RegisterFn)
# The first argument is the JS-facing name (used by `internalBinding('name')`),
# the file containing the macro is the C++ side. Filename conventions don't
# always match (e.g. `fs` lives in `src/node_file.cc`, not `node_fs.cc`), so
# the conventional-name lookup misses real bindings and `projmem reverse`
# under-reports consumers. This scanner closes that gap.
_BINDING_MACRO_RX = re.compile(
    r"\bNODE_BINDING_"
    r"(?:CONTEXT_AWARE_INTERNAL|PER_ISOLATE_INIT|EXTERNAL_REFERENCE)"
    r"\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\b")

# Cached map per root: binding_name -> repo-relative .cc path. Populated
# lazily on first miss in `_resolve_internal_binding`.
_BINDING_MACRO_MAP: Dict[str, Dict[str, str]] = {}


def _scan_binding_macros(root: str) -> Dict[str, str]:
    """Walk src/**/*.{cc,cpp} for NODE_BINDING_* registration macros.
    Returns {binding_name: repo_relative_path}. Bounded to the src/
    subtree — Node's own bindings live there; deps/ do not register
    internal bindings. First-hit wins when a name is registered more
    than once (should not happen in practice).
    """
    found: Dict[str, str] = {}
    src_root = os.path.join(root, "src")
    if not os.path.isdir(src_root):
        return found
    for dirpath, dirnames, filenames in os.walk(src_root):
        # Prune obvious non-source subtrees.
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "build", "out", "node_modules")]
        for fn in filenames:
            if not fn.endswith((".cc", ".cpp", ".cxx")):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
            except OSError:
                continue
            if "NODE_BINDING_" not in src:
                continue
            for m in _BINDING_MACRO_RX.finditer(src):
                name = m.group(1)
                if name and name not in found:
                    rel = os.path.relpath(full, root).replace("\\", "/")
                    found[name] = rel
    return found


def _resolve_internal_binding(name: str, root: str) -> Optional[str]:
    """Map a Node `internalBinding('X')` name to its C++ source file.

    Resolution order:
      1. Scanned NODE_BINDING_* macro map (authoritative — filename may
         not match the binding name, e.g. `fs` → `src/node_file.cc`).
      2. Filename conventions Node tends to follow:
           src/node_<x>.cc, src/<x>.cc, src/crypto/crypto_<x>.cc,
           src/inspector/<x>.cc, src/<x>/<x>.cc
    Returns the resolved repo-relative path, or None.
    """
    s = (name or "").strip().strip('"').strip("'")
    if not s or "/" in s:
        return None
    macro_map = _BINDING_MACRO_MAP.get(root)
    if macro_map is None:
        macro_map = _scan_binding_macros(root)
        _BINDING_MACRO_MAP[root] = macro_map
    if s in macro_map:
        return macro_map[s]
    candidates = [
        f"src/node_{s}.cc",
        f"src/{s}.cc",
        f"src/crypto/crypto_{s}.cc",
        f"src/inspector/{s}.cc",
        # Some bindings live in a subdir of the same name
        f"src/{s}/{s}.cc",
    ]
    for c in candidates:
        if os.path.isfile(os.path.join(root, c)):
            return c.replace("\\", "/")
    return None


def _resolve_c(spec: str, rel_path: str, root: str) -> Optional[str]:
    """Resolve `#include "x.h"` relative to the current file. <...> is treated as system."""
    s = spec.strip()
    if s.startswith("<") and s.endswith(">"):
        return None  # system
    s = s.strip('"')
    if not s:
        return None
    base = os.path.dirname(rel_path)
    cand = os.path.normpath(os.path.join(base, s))
    if os.path.isfile(os.path.join(root, cand)):
        return cand.replace("\\", "/")
    # also try at repo root
    if os.path.isfile(os.path.join(root, s)):
        return s.replace("\\", "/")
    return None


def _resolve_go(spec: str, rel_path: str, root: str) -> Optional[str]:
    """Local-repo package resolution only. External modules → None (recorded as module:...)."""
    s = spec.strip().strip('"')
    if not s:
        return None
    # If the path starts with "./" or "../", resolve relative.
    if s.startswith("./") or s.startswith("../"):
        base = os.path.dirname(rel_path)
        cand = os.path.normpath(os.path.join(base, s))
        if os.path.isdir(os.path.join(root, cand)):
            return cand.replace("\\", "/")
        return None
    # Heuristic: last segment of an external path MAY map to a local dir.
    tail = s.split("/")[-1]
    for dirpath, _, _ in os.walk(root):
        if os.path.basename(dirpath) == tail:
            rel = os.path.relpath(dirpath, root).replace("\\", "/")
            if not rel.startswith(".."):
                return rel
        # bail fast on deep trees
    return None


# ---- main entry ------------------------------------------------------------

def index(store, rel_path: str, src: str, ts_lang: str, root: str) -> bool:
    """Run the tree-sitter query for this language and populate the store.
    Returns True if indexing succeeded; False if the caller should fall back.

    Fall-back causes (set `PROJMEM_DEBUG=1` to print to stderr):
      - tree-sitter not importable
      - grammar compilation failed
      - parser.parse raised (rare; usually encoding)
      - query captures raised (query bug on a novel AST shape)
      - per-file wall-clock timeout hit (adversarial AST shapes, e.g. huge
        template literals with nested `${...}` chains — which is exactly the
        shape pwnpilot's scanner.js has that was wedging 5+ minutes at 100%
        CPU on `QueryCursor.captures`). Timeout is configurable via
        `PROJMEM_TS_TIMEOUT_MS` env var; default 20000ms.
    """
    import os as _os, sys as _sys, signal as _signal
    debug = bool(_os.environ.get("PROJMEM_DEBUG"))
    timeout_ms = int(_os.environ.get("PROJMEM_TS_TIMEOUT_MS", "20000"))

    if not _AVAILABLE:
        if debug: _sys.stderr.write(f"[ts] {rel_path}: tree-sitter not available\n")
        return False
    parser, query = _compile(ts_lang)
    if parser is None or query is None:
        if debug: _sys.stderr.write(f"[ts] {rel_path}: grammar for {ts_lang} unavailable\n")
        return False

    # Watchdog via SIGALRM — POSIX only, main thread only. On non-POSIX or
    # non-main-thread callers, `signal.setitimer` raises ValueError and we
    # proceed without a timeout (matching the previous behavior).
    class _TimeoutErr(Exception): pass

    def _handler(_signum, _frame):
        raise _TimeoutErr()

    installed_timer = False
    prev_handler = None
    try:
        if timeout_ms > 0:
            try:
                prev_handler = _signal.signal(_signal.SIGALRM, _handler)
                _signal.setitimer(_signal.ITIMER_REAL, timeout_ms / 1000.0)
                installed_timer = True
            except (ValueError, AttributeError):
                # Not POSIX / not main thread — proceed without timeout.
                installed_timer = False
        try:
            tree = parser.parse(src.encode("utf-8", errors="replace"))
        except _TimeoutErr:
            if debug: _sys.stderr.write(
                f"[ts] {rel_path}: parse TIMEOUT ({timeout_ms}ms) — falling back to regex\n")
            return False
        except Exception as e:
            if debug: _sys.stderr.write(f"[ts] {rel_path}: parse failed: {e}\n")
            return False
        try:
            cur = QueryCursor(query)
            caps = cur.captures(tree.root_node)
        except _TimeoutErr:
            if debug: _sys.stderr.write(
                f"[ts] {rel_path}: query TIMEOUT ({timeout_ms}ms) — falling back to regex\n")
            return False
        except Exception as e:
            if debug: _sys.stderr.write(f"[ts] {rel_path}: query failed: {e}\n")
            return False
    finally:
        if installed_timer:
            _signal.setitimer(_signal.ITIMER_REAL, 0)
            if prev_handler is not None:
                _signal.signal(_signal.SIGALRM, prev_handler)

    defined: set = set()
    imports: List[Tuple[str, str, int]] = []   # (kind, spec, line)
    refs: List[Tuple[str, int, str]] = []      # (name, line, kind) — call/new/import_binding

    for capname, nodes in caps.items():
        for node in nodes:
            try:
                text = node.text.decode("utf-8", errors="replace")
            except Exception:
                continue
            line = node.start_point[0] + 1
            col = node.start_point[1]

            if capname.startswith("sym."):
                kind = capname.split(".", 1)[1]
                # M2: compute enclosing declaration range. The captured node
                # is the identifier; its parent is usually the declaration
                # (function_declaration, class_declaration, method_definition,
                # lexical_declaration, etc.). Walk up to the nearest
                # declaration-looking ancestor; fallback to the identifier
                # itself if the parent shape is unexpected.
                end_point = node.end_point
                try:
                    par = node.parent
                    # Ascend through chained nodes like `variable_declarator`
                    # → `lexical_declaration` so `const X = ...` captures the
                    # full declaration, not just the identifier.
                    declaration_node_types = {
                        "function_declaration", "function_definition",
                        "class_declaration", "class_definition",
                        "method_definition", "method_declaration",
                        "lexical_declaration", "variable_declaration",
                        "variable_declarator", "interface_declaration",
                        "type_alias_declaration", "enum_declaration",
                        "type_declaration", "type_spec",
                        "function_item", "struct_item", "enum_item",
                        "trait_item", "mod_item", "impl_item",
                        "assignment_expression",
                    }
                    hops = 0
                    while par is not None and hops < 4:
                        if par.type in declaration_node_types:
                            end_point = par.end_point
                            break
                        par = par.parent
                        hops += 1
                except Exception:
                    pass
                store.add_symbol(
                    file=rel_path, name=text, kind=kind,
                    line=line, col=col,
                    end_line=end_point[0] + 1, end_col=end_point[1],
                    exported=int(not text.startswith("_")),
                    confidence="high",
                )
                defined.add(text)

                # Qualified-name aliasing (C++ / Rust path / etc.).
                # `Class::method` — also store under bare `method` so
                # `projmem symbol method` and `projmem symbol Class::method`
                # both resolve. Discovered missing on nodejs/node v22.11.0
                # (see docs/REAL_STRESS_FINAL.md §5). Aliasing is
                # ADDITIVE — the qualified row above remains the
                # canonical record; the bare alias is best-effort with
                # a `qualified_alias` parser tag for downstream filters.
                if "::" in text:
                    bare = text.rsplit("::", 1)[-1]
                    if bare and bare != text:
                        store.add_symbol(
                            file=rel_path, name=bare, kind=kind,
                            line=line, col=col,
                            end_line=end_point[0] + 1,
                            end_col=end_point[1],
                            exported=int(not bare.startswith("_")),
                            confidence="medium",  # alias, not native
                        )
                        defined.add(bare)
            elif capname == "import.path":
                imports.append(("path", text, line))
            elif capname == "import.module":
                imports.append(("module", text, line))
            elif capname == "import.path.reexport_star":
                # `export * from "./foo"` — same resolution path as an
                # ordinary import but tagged as a barrel re-export so
                # reverse_deps can walk it transitively.
                imports.append(("reexport_star", text, line))
            elif capname == "import.path.reexport_named":
                # `export { X } from "./foo"` — named re-export. Treated
                # identically to an import for reverse-dep purposes for now.
                imports.append(("path", text, line))
            elif capname == "import.path.binding":
                # Node-style `internalBinding('X')` — try several
                # filename conventions for the C++ side. Most Node
                # bindings live in `src/node_<x>.cc`; some in
                # `src/<x>.cc` or `src/crypto/crypto_<x>.cc`.
                imports.append(("binding", text, line))
            elif capname == "py_from_stmt":
                # Python `from <mod> import a, b, c` — emit <mod>.<a>, <mod>.<b> …
                # This lets `from . import packs` resolve to projmem/packs.py,
                # which the module-only capture misses.
                # Self-test D5 fix: when `name` is an `aliased_import`
                # (i.e. `from .x import Y as Z`), extract just the bare
                # name `Y`, not the whole `Y as Z` text — otherwise the
                # composite spec becomes `.x.Y as Z` and never resolves.
                mod_node = node.child_by_field_name("module_name")
                if mod_node is not None:
                    try:
                        mod_text = mod_node.text.decode("utf-8", errors="replace")
                    except Exception:
                        mod_text = ""
                    # Align with the stdlib-AST indexer behavior:
                    # - Always record the module itself via the `import.module`
                    #   capture (handled elsewhere).
                    # - Record `<mod>.<name>` ONLY when it resolves to a real
                    #   file path (submodule import), OR when the module itself
                    #   is unresolved (so missing-paths can flag it).
                    # Without this gate, `from .store import Store` would emit
                    # a bogus unresolved `module:.store.Store` edge that is
                    # actually a SYMBOL import, not a file import. That
                    # incorrectly lowers pack integrity for many repos (including
                    # projmem itself).
                    mod_target = _resolve_python(mod_text, rel_path, root) if mod_text else None
                    for child in node.children_by_field_name("name"):
                        try:
                            if child.type == "aliased_import":
                                # Aliased form — recover the bare imported name.
                                name_child = child.child_by_field_name("name")
                                if name_child is None:
                                    continue
                                nm = name_child.text.decode(
                                    "utf-8", errors="replace")
                            else:
                                nm = child.text.decode(
                                    "utf-8", errors="replace")
                        except Exception:
                            continue
                        # Skip if the capture still looks malformed (contains
                        # spaces, e.g. an unhandled alias shape).
                        if " " in nm or not nm:
                            continue
                        sep = "" if mod_text.endswith(".") else "."
                        composite = mod_text + sep + nm
                        member_target = _resolve_python(composite, rel_path, root)
                        if member_target and member_target != mod_target:
                            # Member resolves to a sibling submodule — emit
                            # the concrete file edge.
                            store.add_edge(
                                src=rel_path,
                                dst=member_target,
                                type="imports",
                                confidence="high",
                                evidence=f"from {mod_text or '.'} import {nm}",
                            )
                        # GAP 5 correctness fix (2026-04): do NOT emit a
                        # `module:<mod>.<member>` edge when neither the module
                        # nor the composite resolves. The module-level
                        # unresolved edge (from the `import.module` capture)
                        # already carries that signal; the per-member one
                        # fabricates fake entries like
                        # `module:...missing.Thing` for every imported NAME
                        # in `from ...missing import Thing, Other`, which
                        # pollutes unresolved-imports, integrity scores, and
                        # pack trust. This mirrors the identical fix in
                        # indexer.index_python.
            elif capname in ("rel.extends", "rel.implements"):
                # M3: walk up to the enclosing class/interface declaration to
                # find the subclass name, then emit `implements`/`extends`
                # edge from (file, subclass) → target name.
                subclass = None
                try:
                    p = node.parent
                    hops = 0
                    while p is not None and hops < 6:
                        if p.type in ("class_declaration", "interface_declaration",
                                      "class_definition"):
                            name_node = p.child_by_field_name("name")
                            if name_node is not None:
                                subclass = name_node.text.decode(
                                    "utf-8", errors="replace")
                            break
                        p = p.parent
                        hops += 1
                except Exception:
                    pass
                if subclass:
                    rel_type = "implements" if capname == "rel.implements" else "extends"
                    # M8: build canonical symbol_id for SOURCE; resolve target
                    # name to symbol_id when unambiguous (high confidence) or
                    # fall back to the bare name (medium).
                    from . import symbol_id as _sid
                    src_id = _sid.build(rel_path, subclass, "class")
                    resolved = store.resolve_name_to_symbol_id(
                        text, prefer_kind="class")
                    if not resolved and rel_type == "implements":
                        resolved = store.resolve_name_to_symbol_id(
                            text, prefer_kind="interface")
                    edge_dst = resolved or text
                    edge_conf = "high" if resolved else "medium"
                    store.add_edge(
                        src=src_id,
                        dst=edge_dst,
                        type=rel_type,
                        confidence=edge_conf,
                        evidence=f"{rel_type}: {subclass} {rel_type} {text}",
                    )
                    # Record the parent name as a ref with target_symbol_id when known
                    refs.append((text, line, rel_type))
            elif capname in ("ref.call", "ref.new", "ref.import_binding",
                             "ref.callback", "ref.shorthand", "ref.name"):
                # Ref kinds distinguish usage shape so `orphans` / `parity`
                # can reason correctly. Round-3 added call/new/import_binding;
                # round-4 adds callback (`.map(fn)`) and shorthand
                # (`{name}` inside object literals). Round-5 adds `ref.name`
                # for attribute-access RECEIVERS (`mod` in `mod.hello()`) —
                # without it, cross-file `projmem symbol <module>` lookups
                # silently miss every dotted usage.
                if 2 <= len(text) <= 64:
                    kind_tag = {"ref.call": "call", "ref.new": "new",
                                "ref.import_binding": "import_binding",
                                "ref.callback": "callback",
                                "ref.shorthand": "shorthand",
                                "ref.name": "name"}[capname]
                    refs.append((text, line, kind_tag))
            # unknown captures ignored silently

    # Emit imports as edges
    for _kind, spec, _line in imports:
        if _kind == "binding":
            # Node `internalBinding('X')` → try several C++ filename
            # conventions that Node uses for its bindings.
            target = _resolve_internal_binding(spec, root)
            if target:
                store.add_edge(
                    src=rel_path, dst=target, type="imports",
                    confidence="high",
                    evidence=f"internalBinding('{spec}')")
            else:
                store.add_edge(
                    src=rel_path, dst=f"binding:{spec}",
                    type="imports", confidence="medium",
                    evidence=f"internalBinding('{spec}') — C++ side not found")
            continue
        target = _resolve_import(spec, ts_lang, rel_path, root)
        targets = _expand_target(target, ts_lang, root) if target else [None]
        # Barrel re-exports (`export * from "./foo"`) get two edge types:
        # the normal `imports` edge (so forward_deps works) AND a
        # `reexport_star` edge so reverse_deps can follow the barrel
        # transitively. Without the second edge, a file that is reached
        # only through a namespace barrel (TypeScript's src/compiler/
        # _namespaces/ts.ts is the canonical case) has zero reverse deps
        # and every downstream delete-safety query gives a false "safe".
        is_reexport_star = (_kind == "reexport_star")
        for t in targets:
            if t is None and ts_lang in ("javascript", "typescript", "tsx") \
                    and _is_node_builtin(spec):
                # Known external (Node builtin) — classify explicitly so it
                # does NOT show up in `unresolved-imports`.
                s = spec.strip('"').strip("'")
                store.add_edge(src=rel_path, dst=f"builtin:node:{s}",
                               type="imports", confidence="high",
                               evidence=f"node builtin: {s}")
                continue
            store.add_edge(
                src=rel_path,
                dst=t or f"module:{spec.strip('\"').strip('<').strip('>')}",
                type="imports",
                confidence="high" if t else "medium",
                evidence=f"import {spec}",
            )
            if is_reexport_star and t:
                store.add_edge(
                    src=rel_path,
                    dst=t,
                    type="reexport_star",
                    confidence="high",
                    evidence=f"export * from {spec}",
                )

    # Emit refs (call/new/import_binding). High confidence since they're
    # AST-grounded. De-duplicate on (name, line, kind).
    seen: set = set()
    for name, line, kind_tag in refs:
        if (name, line, kind_tag) in seen:
            continue
        seen.add((name, line, kind_tag))
        store.add_ref(file=rel_path, name=name, kind=kind_tag, line=line,
                      confidence="high")

    # Native binding edges (C/C++): detect explicit JS-facing names bound to
    # native function identifiers (e.g. Node/V8 SetMethod helpers). These are
    # NOT speculative — only emitted when the registration call contains a
    # string literal name and a direct identifier function pointer.
    if ts_lang in ("c", "cpp"):
        try:
            from . import bindings as _bindings
            for b in _bindings.extract_cpp_bindings(src):
                cpp_raw = str(b.get("cpp_name") or "")
                cpp_bare = cpp_raw.rsplit("::", 1)[-1] if cpp_raw else cpp_raw
                cpp_symbol_id = None
                # Prefer same-file resolution to avoid same-name traps.
                row = store.conn.execute(
                    "SELECT symbol_id, name FROM symbols "
                    "WHERE file=? AND kind='function' AND name IN (?, ?) "
                    "ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END "
                    "LIMIT 1",
                    (rel_path, cpp_raw, cpp_bare, cpp_raw),
                ).fetchone()
                if row and row["symbol_id"]:
                    cpp_symbol_id = row["symbol_id"]
                    cpp_bare = row["name"] or cpp_bare
                store.add_binding(
                    file=rel_path,
                    line=int(b.get("line") or 0),
                    js_name=str(b.get("js_name") or ""),
                    cpp_name=str(cpp_bare or cpp_raw),
                    cpp_symbol_id=cpp_symbol_id,
                    confidence=float(b.get("confidence") or 0.9),
                    reason=str(b.get("reason") or ""),
                    evidence=str(b.get("evidence") or ""),
                )
        except Exception:
            pass  # never let binding extraction break indexing

    return True


def _resolve_ruby(spec: str, rel_path: str, root: str) -> Optional[str]:
    """Resolve `require_relative 'x'` (relative to file) and bare `require 'x'`
    (relative to root) to a .rb file if one exists."""
    s = spec.strip().strip('"').strip("'")
    if not s:
        return None
    cands = []
    if s.startswith(".") or "/" in s:
        base = os.path.dirname(rel_path)
        c = os.path.normpath(os.path.join(base, s))
        cands += [c + ".rb", c]
    else:
        # require 'util' → try ./util.rb next to file, then repo root.
        base = os.path.dirname(rel_path)
        cands += [os.path.normpath(os.path.join(base, s)) + ".rb",
                  s + ".rb"]
    for c in cands:
        if os.path.isfile(os.path.join(root, c)):
            return c.replace("\\", "/")
    return None


_PKG_EXTS = {"go": (".go",), "rust": (".rs",), "java": (".java",)}


def _expand_target(target: str, ts_lang: str, root: str) -> List[str]:
    """If `target` is a package directory for a package-based language, expand
    to each source file inside it. Otherwise return [target] unchanged."""
    full = os.path.join(root, target)
    if not os.path.isdir(full):
        return [target]
    exts = _PKG_EXTS.get(ts_lang)
    if not exts:
        return [target]
    out = []
    for name in sorted(os.listdir(full)):
        if name.endswith(exts):
            out.append(os.path.join(target, name).replace("\\", "/"))
    return out or [target]


def _resolve_import(spec: str, ts_lang: str, rel_path: str, root: str) -> Optional[str]:
    if ts_lang == "python":
        return _resolve_python(spec, rel_path, root)
    if ts_lang in ("javascript", "typescript", "tsx"):
        # Strip surrounding quotes if they leaked through
        return _resolve_js(spec.strip('"').strip("'"), rel_path, root)
    if ts_lang in ("c", "cpp"):
        return _resolve_c(spec, rel_path, root)
    if ts_lang == "go":
        return _resolve_go(spec, rel_path, root)
    if ts_lang == "ruby":
        return _resolve_ruby(spec, rel_path, root)
    return None  # java/csharp/kotlin/swift/etc: record as module:
