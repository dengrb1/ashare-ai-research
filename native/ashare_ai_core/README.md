# ashare_ai_core

Optional Rust kernels for the A-share research pipeline. The crate deliberately does not
perform PIT filtering, database access, or trading-rule decisions. Python freezes and
validates the input arrays first, then may call this kernel for numeric calculations.

## Pure Rust tests

```powershell
cargo test --manifest-path native/ashare_ai_core/Cargo.toml
```

## Build the optional Python extension

Install `maturin`, then build from the repository root:

```powershell
maturin develop --manifest-path native/ashare_ai_core/Cargo.toml --features python
```

The Python facade uses the extension only when `ASHARE_NATIVE_TECHNICAL=auto` (default) or
`on`. Set `ASHARE_NATIVE_TECHNICAL=off` to force the reference implementation. If the
extension is unavailable, `auto` falls back to Python; `on` raises an explicit error.
