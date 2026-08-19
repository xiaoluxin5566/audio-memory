# Third-party notices

## FFmpeg

Audio Memory release archives include `ffmpeg` and `ffprobe`, built from the unmodified FFmpeg 8.0.1 source release at <https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz>.

The bundled build disables GPL and nonfree components and is distributed under FFmpeg's LGPL 2.1-or-later terms. Its exact source URL, source SHA-256, configure flags, and binary SHA-256 values are recorded in `runtime/ffmpeg/manifest.json`. The corresponding license text is included in `runtime/ffmpeg/LICENSE.md`.

FFmpeg source code and redistribution guidance are available at:

- <https://ffmpeg.org/download.html>
- <https://ffmpeg.org/legal.html>

## uv

Audio Memory release archives include the Apple Silicon `uv` executable used only to create the application's private Python environment during installation. uv is maintained by Astral and distributed under the Apache-2.0 and MIT licenses: <https://github.com/astral-sh/uv>.
