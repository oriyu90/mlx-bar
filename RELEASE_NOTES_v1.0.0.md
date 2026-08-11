# MLXBar v1.0.0

MLXBar v1.0.0 is the first stable release for Apple Silicon Macs.

## Highlights

- Resizable Settings window with a larger minimum size so controls are no longer clipped.
- English is now the default GUI language; Japanese is available in Settings > General > Language.
- Runtime install, update, rollback, cancellation, history, and removal now live in Settings > Runtime.
- Missing `mlx-lm` and `mlx-vlm` runtimes are installed automatically in background jobs on startup.
- OpenAI-compatible Chat Completions streaming now uses stable chunk timestamps, one terminal chunk, consistent OpenAI-shaped validation errors, heartbeat/keep-alive behavior, and a reliable `[DONE]` terminator.

## Verification

- 62 unit and contract tests passed.
- 6 coordinator end-to-end tests passed with real local listeners.
- Swift debug and release builds passed.
- App signature, bundled resources, launch agent, packaged coordinator/CLI, DMG structure, and SHA-256 checksum passed verification.

SHA-256 (`MLXBar-1.0.0.dmg`):

`d9c709f222387cf9b09d31d9b2596ce93f5d9073217b129a8369d1d0bec820f0`

The included build is ad-hoc signed. On first launch, macOS may require approval in System Settings > Privacy & Security.
