# craft-pdf

Shared PDF writing primitives for Nova Foods applications. Applications retain document models,
layout, shaping, font selection and licensing, tagging, colour policy, print preparation,
export presets and postprocessing. There is no GUI or application dependency.

Features are explicit: `default = []`; `write` enables authoring; `write-pdf` additionally
supports embedding existing PDF pages. Nova Word, Nova Deck and Nova Vector use `write`.
Nova Design uses `write-pdf` for placed PDF pages and book composition. Plain `write` does
not enable Hayro parsing, interpretation or its image codecs; Krilla's existing raster-image
and simple-text support remain enabled. Reading dependencies used to inspect test outputs
are dev-only. A reader migration can be reviewed and adopted separately.

The writer pins Krilla 0.8.2, with a local unwind-cleanup patch documented in
[`vendor/krilla/README.craft.md`](../../vendor/krilla/README.craft.md). Successful
serialization uses the same backend version as before extraction. It provides:

- `write::configuration`: version and archival validation configuration with typed failures.
- `write::Metadata` and `write::Writer`: common metadata setup and consuming serialization;
  compression, colour profiles and other serialization settings pass through unchanged.
  `Writer::new_with_language` also accepts an optional document language for the catalog
  and XMP metadata; empty language is treated as absent, while callers retain
  selection and validation policy for nonempty tags.
- `write::font`: fallible font copying/preparation for subset embedding, with a 512 MiB limit
  per font, at most 256 variation coordinates, and rejection of non-finite coordinates.
- `write::guard`: a native unwind guard for the whole app export, including drawing and
  postprocessing. App errors pass through; serialization errors retain backend diagnostics.
  Caught panic messages retain up to 1024 Unicode scalar values; the installed panic hook
  remains responsible for logging source locations and full diagnostics.

`Writer::document_mut` and `write::backend` deliberately expose Krilla's page, resource and
painting APIs. The apps keep their document walks, pagination, font selection/licensing,
outline fallbacks and warnings, colour policy, PDF/X postprocessing, tagging semantics,
links, export presets and native editing payloads. Shared writing does not introduce a
universal document model or claim additional format conformance. Dates and branding are
provided by callers; the shared writer does not read a clock.

`Writer::finish` consumes its document even on error. When `guard` returns an internal failure,
discard any writer or drawing state captured by the closure; construct it inside the closure
where possible. Failed page/surface cleanup poisons the backend document, so retaining it
after a narrow catch cannot silently serialize a missing page. This also rejects failure
during explicit surface finalization. Other captured app state must still be discarded.
Native unwind recovery cannot catch WASM aborts or process-wide out-of-memory
failures. Font preparation bounds do not impose a total memory, output-size or time budget on
PDF serialization. Individual unsupported fonts still follow each app's existing outline
fallback and warning behavior.

Run `cargo test -p craft-pdf --no-default-features --features write`, and repeat with
`--features write-pdf` and `--all-features`. The same writer contracts run with
`--target wasm32-unknown-unknown` and a matching `wasm-bindgen-test-runner`. They exercise
metadata, page sizes, versions, reproducibility, typed validation failures, malformed fonts,
application error preservation, embedded pages, and native-only panic recovery.

## Source pinning and local development

Consumers pin both the facade and patched writer to the same full commit:

```toml
[workspace.dependencies]
craft-pdf = { git = "https://github.com/EuLucaDev/novafoods-libs", rev = "<full-commit-id>", version = "=0.1.0", default-features = false, features = ["write"] }

[patch.crates-io]
krilla = { git = "https://github.com/EuLucaDev/novafoods-libs", rev = "<full-commit-id>", version = "=0.8.2" }
```

Choose `write-pdf` only when embedding PDF pages. Cargo ignores dependency workspaces'
root patches; copy the Krilla patch into the consuming workspace. A compile-time marker
rejects unpatched Krilla. Check the normal dependency graph separately from the test graph
and ensure there is one Krilla source, including uses through `krilla-svg`. This writer
migration does not change an application's reader backend or its other novafoods-libs pins.
The selected revision must be remotely available before fresh consumers can resolve it.
Local path replacements are development-only and should not be committed to an app.

Run `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings` and
`cargo test --workspace`, then test `craft-pdf` with `--features write` and
`--features write-pdf` (the default feature set intentionally enables no PDF operations).
Run the same writer contracts on `wasm32-unknown-unknown` with a matching
`wasm-bindgen-test-runner`. Native recovery tests are subprocess or unwind checks;
WASM tests exercise successful paths and typed input errors, not recovery from aborts.

## Unicode and font contracts

The writer embeds the font and glyph run supplied by the application. Applications
must select a font that covers the requested characters, shape text and resolve
bidirectional layout before drawing. Passing correct Unicode only in document
metadata does not preserve page text. A selectable-text export must retain the
actual glyph outlines and a matching ToUnicode map, and applications must report
unsupported font preparation rather than silently dropping characters. Existing
app-specific outline fallback remains an explicit policy choice.

`tests/unicode_writing.rs` adds opt-in, externally supplied font fixtures. It exports,
reopens and interprets real page text without putting the source text in metadata,
checks nonzero glyph IDs, embedded fonts and visible non-space outlines, and asserts
exact extraction for Latin accents, Greek, Cyrillic, Vietnamese, Arabic and Thai.
A variable-font test checks default/empty coordinates and distinct light/bold
embedded outlines while preserving extraction. These are embedding/mapping
contracts; they do not qualify Arabic joining, RTL caret behavior, shaping quality,
or lossless text editing in an application's separate COS editor.

No font files are stored in this repository. Obtain the licensed fixtures listed in
[`tests/fonts.json`](tests/fonts.json), verify their SHA-256 hashes, place them in
one directory using the listed names, then run:

```sh
CRAFT_PDF_TEST_FONT_DIR=/absolute/path/to/licensed-fonts \
RUSTFLAGS="--cfg craft_pdf_unicode_fixtures" \
cargo test -p craft-pdf --all-features --test unicode_writing
```

Repeat with `--target wasm32-unknown-unknown` and the matching test runner for
runtime WASM coverage. Ordinary builds and tests do not require these fonts or the
fixture environment variable. With write enabled, the complete test code is
compiled even without fixtures; its two runtime tests are explicitly ignored with a
provisioning reason. Running ignored tests without fixtures fails with setup instructions.
These contracts require explicit execution in addition to the ordinary test suite.
