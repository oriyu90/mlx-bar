# MLXBar v1.0.0 implementation and release plan

Date: 2026-08-11

## Scope and acceptance criteria

1. Settings window
   - Remove the fixed 640×480 content size.
   - Open with at least 820×620 points and allow the window to grow.
   - Keep every settings section reachable through its scrolling container.
2. GUI language
   - Start in English for new and existing installations without a language preference.
   - Offer English and Japanese under Settings > General > Language.
   - Persist the selection in `general.language` and reject unsupported values.
3. Runtime lifecycle
   - Remove runtime-update controls from the menu-bar popover.
   - Put install, update, rollback, history, cancellation, and removal in Settings > Runtime.
   - On service startup, schedule background installation jobs for each missing `mlx-lm` and `mlx-vlm` runtime without blocking the UI.
   - Reuse the same guarded job path for automatic and manual updates.
4. OpenAI-compatible API stability
   - Return OpenAI-shaped errors for request-validation failures.
   - Validate stream options and token types before model execution.
   - Use one stable ID/timestamp per stream and one terminal completion chunk.
   - Preserve heartbeat and proxy-buffering protections, and end successful streams with `[DONE]`.

## Verification

- Python unit and contract suite: 62 passed.
- Coordinator end-to-end suite using real local listeners: 6 passed.
- Swift source syntax parse: passed for all application sources.
- Swift debug and release builds: passed with the matching macOS 15.4 SDK supplied by the installed Command Line Tools.
- English and Japanese `.strings` validation: passed.
- Distribution verification: app contents, launch agent property list, ad-hoc signature, DMG checksum, and packaged coordinator/CLI smoke tests passed.

## Release procedure

1. Run all verification above on the release host.
2. Build and sign `MLXBar.app` and create `MLXBar-1.0.0.dmg` with `scripts/build-release.sh`.
3. Verify the app, launch agent, signature, resources, and DMG with `scripts/verify-release.sh`.
4. Commit and push the release changes.
5. Create or update the GitHub `v1.0.0` release with the verified DMG and release notes.
