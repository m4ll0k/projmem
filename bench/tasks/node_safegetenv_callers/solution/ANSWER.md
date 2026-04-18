# Callers of credentials::SafeGetenv

The wrapper is invoked at the following call sites in the C++ source.
Each line below is a distinct compilation unit that issues at least
one `credentials::SafeGetenv(...)` call.

caller: src/crypto/crypto_context.cc
caller: src/node.cc
caller: src/debug_utils.cc
caller: src/path.cc
caller: src/node_options.cc
caller: src/env.cc

The header (declares the prototype) and the implementation file (provides
the body) are intentionally excluded — they are not call sites.
