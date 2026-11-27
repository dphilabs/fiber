# fiber.node.worker.daily.media
"""@desc: Inspect, trim and convert audio/video files under a workspace root. Requires `ffprobe` / `ffmpeg`."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Dict, List, Optional

from xphi.arch.contract.protocol.binder import ToolError, tool
from xphi.arch.contract.protocol.worker import AsyncWorkerProtocol

__deps__ = {"bins": ["ffmpeg", "ffprobe"]}
FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")
FFMPEG_HINT = "ffmpeg/ffprobe is not installed (macOS: brew install ffmpeg, Debian/Ubuntu: apt install ffmpeg)."

PROBE_TIMEOUT_SEC = 30
_TIME_RE = re.compile(r"^(\d+(\.\d+)?|\d{1,2}:\d{2}:\d{2}(\.\d+)?)$")  # seconds or HH:MM:SS(.ms)
_AUDIO_FORMATS = {"mp3", "wav", "m4a", "aac", "flac", "ogg"}
_VIDEO_FORMATS = {"mp4", "mkv", "webm", "mov", "gif"}


async def _run(cmd: List[str], timeout: int) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"{Path(cmd[0]).name} timed out after {timeout}s.") from None
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-5:]
        raise ToolError(f"{Path(cmd[0]).name} failed: {' | '.join(tail)}")
    return out.decode("utf-8", "replace")


async def _run_to_output(cmd: List[str], out_path: Path, timeout: int) -> None:
    """ffmpeg 실패·타임아웃·취소 시 생성 중이던 미완성 출력 파일을 삭제해 재시도 차단을 방지합니다 (MD-2)."""
    try:
        await _run(cmd, timeout)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise


def _seconds(value: str, name: str) -> float:
    value = value.strip()
    if not _TIME_RE.match(value):
        raise ToolError(f"'{name}' must be seconds or HH:MM:SS(.ms), got {value!r}")
    if ":" not in value:
        return float(value)
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _fps(rate: Optional[str]) -> Optional[float]:
    if not rate or rate == "0/0":
        return None
    num, _, den = rate.partition("/")
    try:
        return round(float(num) / float(den or 1), 3)
    except (ValueError, ZeroDivisionError):
        return None


class MediaWorker(AsyncWorkerProtocol):
    def __init__(self, root: str = ".", encode_timeout_sec: int = 900) -> None:
        self.root = Path(root).expanduser().resolve()
        self.encode_timeout_sec = max(1, int(encode_timeout_sec))
        super().__init__(agent_name="worker.media")
        if FFMPEG is None or FFPROBE is None:
            self.log.warning(FFMPEG_HINT)

    def _resolve(self, rel: str, *, must_exist: bool = True) -> Path:
        clean = rel.strip()
        if not clean:
            raise ToolError("File path must not be empty.")
        target = (self.root / clean).resolve()
        if not target.is_relative_to(self.root) or target == self.root:
            raise ToolError("Path is outside the workspace root.")
        if must_exist and not target.is_file():
            raise ToolError(f"File not found: {rel}")
        return target

    def _output_path(self, src: Path, output: str, tag: str, suffix: str) -> Path:
        if output:
            out = self._resolve(output, must_exist=False)
            if out.exists():
                raise ToolError(f"Output already exists: {output}")
            out.parent.mkdir(parents=True, exist_ok=True)
            return out
        out = src.with_name(f"{src.stem}_{tag}{suffix}")
        n = 1
        while out.exists():  # never overwrite: pick the next free name
            out = src.with_name(f"{src.stem}_{tag}_{n}{suffix}")
            n += 1
        return out

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    async def _probe_raw(self, src: Path) -> Dict[str, Any]:
        if FFPROBE is None:
            raise ToolError(FFMPEG_HINT)
        out = await _run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src)],
            PROBE_TIMEOUT_SEC,
        )
        try:
            return json.loads(out or "{}")
        except json.JSONDecodeError as exc:
            raise ToolError(f"Failed to parse ffprobe output: {exc}") from None

    @tool
    async def probe(self, path: str) -> Dict[str, Any]:
        """Summarize a media file: duration, size, container and per-stream codec details.

        Args:
            path: Media file relative to the workspace root.
        """
        src = self._resolve(path)
        raw = await self._probe_raw(src)
        fmt = raw.get("format", {})
        streams = []
        for s in raw.get("streams", []):
            item: Dict[str, Any] = {
                "index": s.get("index"),
                "type": s.get("codec_type"),
                "codec": s.get("codec_name"),
            }
            if s.get("codec_type") == "video":
                item.update(width=s.get("width"), height=s.get("height"), fps=_fps(s.get("avg_frame_rate")))
            elif s.get("codec_type") == "audio":
                item.update(sample_rate=int(s.get("sample_rate") or 0) or None, channels=s.get("channels"))
            streams.append(item)
        return {
            "path": path,
            "container": fmt.get("format_name"),
            "duration_sec": round(float(fmt["duration"]), 3) if fmt.get("duration") else None,
            "size_bytes": int(fmt["size"]) if fmt.get("size") else src.stat().st_size,
            "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
            "streams": streams,
        }

    @tool
    async def trim(
        self,
        path: str,
        start: str = "0",
        end: str = "",
        duration: str = "",
        reencode: bool = False,
        output: str = "",
    ) -> Dict[str, Any]:
        """Cut a segment into a new file. Stream copy by default (fast, cuts snap to keyframes).

        Args:
            path: Source media file relative to the workspace root.
            start: Segment start (seconds or HH:MM:SS).
            end: Segment end (seconds or HH:MM:SS). Use either end or duration.
            duration: Segment length (seconds or HH:MM:SS).
            reencode: Re-encode for frame-accurate cuts (slower).
            output: Output path relative to the root (default: '<name>_trim<ext>' next to the source).
        """
        if FFMPEG is None:
            raise ToolError(FFMPEG_HINT)
        if end and duration:
            raise ToolError("Use either 'end' or 'duration', not both.")
        src = self._resolve(path)
        t0 = _seconds(start, "start")
        length = _seconds(duration, "duration") if duration else (_seconds(end, "end") - t0 if end else None)
        if length is not None and length <= 0:
            raise ToolError("Segment length must be positive.")

        out = self._output_path(src, output, "trim", src.suffix)
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-n", "-ss", f"{t0:.3f}", "-i", str(src)]
        if length is not None:
            cmd += ["-t", f"{length:.3f}"]
        cmd += [] if reencode else ["-c", "copy"]
        cmd += [str(out)]
        await _run_to_output(cmd, out, self.encode_timeout_sec)
        return {"output": self._rel(out), "start_sec": t0, "duration_sec": length, "reencoded": reencode}

    @tool
    async def convert(self, path: str, format: str, output: str = "") -> Dict[str, Any]:
        """Convert a media file to another container/format (e.g. mp4 -> mp3, mov -> mp4).

        Args:
            path: Source media file relative to the workspace root.
            format: Target format: mp4, mkv, webm, mov, gif, mp3, wav, m4a, aac, flac, ogg.
            output: Output path relative to the root (default: '<name>_conv.<format>' next to the source).
        """
        if FFMPEG is None:
            raise ToolError(FFMPEG_HINT)
        fmt = format.strip().lower().lstrip(".")
        if fmt not in _AUDIO_FORMATS | _VIDEO_FORMATS:
            raise ToolError(
                f"Unsupported format {format!r}. Use one of: {sorted(_AUDIO_FORMATS | _VIDEO_FORMATS)}"
            )
        src = self._resolve(path)
        out = self._output_path(src, output, "conv", f".{fmt}")

        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-n", "-i", str(src)]
        cmd += ["-vn"] if fmt in _AUDIO_FORMATS else []   # drop video for audio targets
        cmd += [str(out)]                                  # codecs chosen by ffmpeg from the extension
        await _run_to_output(cmd, out, self.encode_timeout_sec)
        return {"output": self._rel(out), "format": fmt, "size_bytes": out.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--timeout", type=int, default=900, help="FFmpeg encode/trim timeout in seconds")
    args = parser.parse_args()
    worker = MediaWorker(root=args.root, encode_timeout_sec=args.timeout)
    try:
        asyncio.run(worker.serve_forever_async())
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()