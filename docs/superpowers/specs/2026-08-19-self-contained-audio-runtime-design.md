# Self-contained Audio Runtime Design

## Goal

Audio Memory's macOS Apple Silicon release must accept valid MP3 and AAC files immediately after installation, without Homebrew or a system-installed FFmpeg.

## Current failure

The release installs its own Python environment but calls `ffmpeg` and `ffprobe` by bare command name. macOS does not include those tools, and a LaunchAgent does not inherit an interactive shell's Homebrew path. A missing `ffprobe` therefore raises before the upload validator can return a format result; the client then displays the service failure as if the audio format were invalid.

## Distribution design

The release builder consumes a prepared Apple Silicon runtime directory containing executable `ffmpeg` and `ffprobe`, plus a manifest with version, source URL, source archive SHA-256, build configuration, binary SHA-256 values, and license classification. The release archive contains these files under `runtime/ffmpeg/bin` and `runtime/ffmpeg/manifest.json`. Release creation fails if either binary is missing, is not arm64, cannot run, or does not match the manifest.

The preferred runtime is built from a pinned official FFmpeg source release with Apple clang. The configuration must keep `--disable-gpl` and `--disable-nonfree`; no external codec libraries are needed for decoding MP3/AAC and producing PCM WAV. The source archive, matching source checksum, FFmpeg license texts, build script, and exact configuration remain available with the release materials. FFmpeg itself states that it distributes source rather than official macOS executables and that redistribution obligations depend on the selected build configuration.

The binary is prepared during release production, not on the end user's machine. The installer performs no Homebrew installation and makes no network request for FFmpeg.

## Installation and runtime resolution

`install-release.sh` treats the bundled runtime and manifest as required release files. Before switching the atomic `current` link, it verifies executable permissions, architecture, version execution, and recorded SHA-256 values. A failed verification leaves the previously installed version selected.

`start.sh` exports the installed runtime `bin` directory at the front of `PATH` and records its explicit location in `AUDIO_MEMORY_FFMPEG` and `AUDIO_MEMORY_FFPROBE`. Backend code resolves tools through one focused module: explicit environment override first, bundled runtime second, system `PATH` only for source-development compatibility. Production release startup requires the bundled paths and never silently falls back to Homebrew.

All upload probing and transcription/diarization subprocess calls use this resolver. Missing or invalid runtime tools produce a stable dependency error rather than `unsupported_format`; the UI only labels a file unsupported when the backend returns that exact code.

## Dependency installation behavior

The extracted terminal release remains responsible for creating its private Python environment and downloading the pinned MLX/diarization models. Existing valid assets are reused. FFmpeg is always copied from the release archive and verified locally; it is never re-downloaded during install.

## Verification

Automated tests must prove:

- the release archive contains both binaries, manifest, license, and build provenance;
- an archive missing or tampering with either binary is rejected;
- installation is idempotent and does not replace the current version after runtime validation failure;
- LaunchAgent startup finds the bundled tools with a minimal system `PATH`;
- upload probing reports a dependency error when the tool is missing and accepts real MP3/AAC with the bundled tool;
- the client does not mark generic service errors as unsupported formats.

Release acceptance additionally runs `file`, `ffmpeg -version`, `ffprobe -version`, a generated MP3 upload, and a generated AAC upload on a clean Apple Silicon account without Homebrew.

## Non-goals

This change does not create a `.app`/DMG, add auto-update, install Homebrew, or broaden supported operating systems and CPU architectures.

## Sources

- FFmpeg download page: <https://ffmpeg.org/download.html>
- FFmpeg license: <https://ffmpeg.org/doxygen/7.0/md_LICENSE.html>
- FFmpeg legal and redistribution guidance: <https://www.ffmpeg.org/legal.html>
