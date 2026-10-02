#!/usr/bin/env python3
"""Portable local ASR wrapper for whisper.cpp.

The script deliberately contains no machine-specific paths or download logic.
Runtime and model are supplied explicitly (or through environment variables).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, text=True, capture_output=capture)


def require_program(name: str, override: str | None = None) -> str | None:
    return str(Path(override).expanduser()) if override else shutil.which(name)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def probe(ffprobe: str, media: Path) -> dict:
    cmd = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(media)]
    p = run(cmd)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "ffprobe failed")
    return json.loads(p.stdout)


def extract_audio(ffmpeg: str, media: Path, wav: Path, start: float = 0.0, duration: float | None = None) -> None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(media)]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-y", str(wav)]
    p = run(cmd)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "ffmpeg audio extraction failed")


def parse_whisper_json(path: Path, offset: float = 0.0, chunk_id: str = "c0001") -> list[dict]:
    """把一块 whisper.cpp 原生 JSON 转成统一片段结构。

    `id` 不在这里生成 —— 分块时每块都从 1 开始编号会撞车，统一由 `write_outputs` 全局编号。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("transcription", data.get("segments", []))
    out = []
    for row in rows:
        text = str(row.get("text", "")).strip()
        if not text:
            continue
        def sec(key: str) -> float:
            value = row.get(key, 0)
            if isinstance(value, str):
                value = value.replace(",", ".")
                if value.endswith("ms"):
                    return float(value[:-2]) / 1000
            value = float(value)
            # Numeric start/end are seconds; explicit offsets are milliseconds.
            return value
        offsets = row.get("offsets", {})
        timestamps = row.get("timestamps", {})
        start = float(offsets.get("from", 0)) / 1000 if offsets else sec("offsets_start")
        end = float(offsets.get("to", 0)) / 1000 if offsets else sec("offsets_end")
        if timestamps:
            def timestamp_value(value: object) -> float:
                s = str(value).replace(",", ".")
                parts = s.split(":")
                if len(parts) == 3:
                    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                return float(s)
            start, end = timestamp_value(timestamps.get("from", 0)), timestamp_value(timestamps.get("to", 0))
        if "start" in row:
            start = sec("start")
        if "end" in row:
            end = sec("end")
        out.append({"id": None, "start": round(start + offset, 3), "end": round(end + offset, 3), "text": text, "source_span": None, "chunk_id": chunk_id, "speaker": None, "words": None, "quality_flags": []})
    return out


def write_outputs(outdir: Path, media: Path, probe_data: dict, segments: list[dict], raw_json: Path, config: dict, elapsed: float) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    # 片段 id 全局唯一 —— 分块时每块各自从 1 编号会撞车，统一在这里重编。
    for n, s in enumerate(segments, 1):
        s["id"] = f"s{n:06d}"
    text = "\n\n".join(s["text"] for s in segments) + ("\n" if segments else "")
    source = outdir / "原文.md"
    source.write_text(text, encoding="utf-8", newline="\n")
    cursor = 0
    for s in segments:
        pos = text.find(s["text"], cursor)
        if pos < 0:
            raise RuntimeError(f"cannot map segment text to 原文.md: {s['id']}")
        s["source_span"] = {"start": pos, "end": pos + len(s["text"])}
        cursor = pos + len(s["text"])
    (outdir / "带时间戳.md").write_text("\n\n".join(f"[{s['start']:.3f}–{s['end']:.3f}] {s['text']}" for s in segments) + ("\n" if segments else ""), encoding="utf-8")
    payload = {"schema_version": 1, "timebase": "source_media_seconds", "source_text_file": "原文.md", "source_text_sha256": sha256(source), "segments": segments}
    (outdir / "segments.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    duration = float(probe_data.get("format", {}).get("duration", 0) or 0)
    quality = {"status": "completed", "duration_sec": duration, "segment_count": len(segments), "elapsed_sec": round(elapsed, 3), "warnings": []}
    source_start = float(config.get("source_start_sec", 0) or 0)
    requested_duration = config.get("source_duration_sec")
    source_end = min(duration, source_start + float(requested_duration)) if requested_duration else duration
    invalid = [s["id"] for s in segments if s["start"] < source_start - 0.01 or s["end"] < s["start"] or s["end"] > source_end + 0.01]
    if invalid:
        quality["status"] = "needs_review"
        quality["warnings"].append(f"{len(invalid)} segment(s) fall outside requested source window; inspect chunk boundaries")
    if not segments and duration:
        quality["status"] = "needs_review"
        quality["warnings"].append("no non-empty ASR segments")
    (outdir / "asr-quality.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"schema_version": 1, "source_media": str(media), "source_media_sha256": sha256(media), "probe": probe_data, "config": config, "raw_result": raw_json.name, "outputs": ["原文.md", "带时间戳.md", "segments.json", "asr-quality.json"]}
    (outdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def doctor(args: argparse.Namespace) -> int:
    failed = False
    for label, name, override in [("ffmpeg", "ffmpeg", args.ffmpeg), ("ffprobe", "ffprobe", args.ffprobe), ("whisper-cli", "whisper-cli", args.whisper_cli)]:
        path = require_program(name, override)
        if not path or not Path(path).exists() and not shutil.which(path):
            print(f"{label}: MISSING", file=sys.stderr)
            failed = True
            continue
        # ffmpeg / ffprobe 用 -version，whisper-cli 只认 --version
        try:
            p = subprocess.run([path, "--help"] if label == "whisper-cli" else [path, "-version"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"{label}: {error}", file=sys.stderr)
            failed = True
            continue
        failed = failed or p.returncode != 0
        print(f"{label}: {path}\n{(p.stdout or p.stderr).splitlines()[0] if (p.stdout or p.stderr) else ''}")
    if not args.model:
        print("model: MISSING", file=sys.stderr)
        failed = True
    else:
        model = Path(args.model).expanduser()
        if not model.is_file() or model.stat().st_size == 0:
            print("model: MISSING or EMPTY", file=sys.stderr)
            failed = True
        else:
            print(f"model: {model} ({model.stat().st_size} bytes, sha256={sha256(model)})")
    print("Environment check only; run a real short transcription to verify model loading.")
    return 1 if failed else 0


def transcribe(args: argparse.Namespace) -> int:
    if args.start < 0 or any(v is not None and v <= 0 for v in (args.duration, args.chunk_seconds, args.threads)):
        raise SystemExit("start must be non-negative; duration, chunk-seconds and threads must be positive")
    if not args.model:
        raise SystemExit("--model is required (or set LOCAL_ASR_MODEL)")
    media = Path(args.media).expanduser().resolve()
    outdir = Path(args.out).expanduser().resolve()
    model = Path(args.model).expanduser().resolve()
    if not media.is_file() or not model.is_file():
        raise SystemExit("media and model must be existing local files; no network fallback is attempted")
    ffmpeg = require_program("ffmpeg", args.ffmpeg); ffprobe = require_program("ffprobe", args.ffprobe); whisper = require_program("whisper-cli", args.whisper_cli)
    if not all([ffmpeg, ffprobe, whisper]):
        raise SystemExit("ffmpeg, ffprobe and whisper-cli are required")
    outdir.mkdir(parents=True, exist_ok=True)
    p = probe(ffprobe, media)
    total_duration = float(p.get("format", {}).get("duration", 0) or 0)
    if total_duration <= 0 or args.start >= total_duration:
        raise SystemExit("requested start must fall inside a media file of known positive duration")
    if not any(x.get("codec_type") == "audio" for x in p.get("streams", [])):
        raise SystemExit("source media has no audio stream")
    requested_end = min(total_duration, args.start + args.duration) if args.duration else total_duration
    if requested_end - args.start < 0.1:
        raise SystemExit("requested audio range is shorter than 0.1 seconds")
    chunk_seconds = args.chunk_seconds or (requested_end - args.start)
    chunks = []
    cursor = args.start
    # Container duration can exceed the final decodable audio by a few milliseconds.
    # Do not pass such an empty tail to whisper-cli as a separate WAV chunk.
    while cursor < requested_end - 1e-6:
        if chunks and requested_end - cursor < 0.1:
            break
        end = min(requested_end, cursor + chunk_seconds)
        chunks.append((cursor, end))
        cursor = end
    identity = {"source_sha256": sha256(media), "model_sha256": sha256(model),
                "language": args.language, "translate": args.translate, "start": args.start,
                "end": requested_end, "chunk_seconds": chunk_seconds, "no_gpu": args.no_gpu,
                "threads": args.threads, "whisper_cli": str(whisper)}
    identity_path = outdir / "resume-identity.json"
    raw_exists = any((outdir / "raw").glob("chunk-*.json"))
    if args.resume and raw_exists:
        if not identity_path.is_file() or json.loads(identity_path.read_text()) != identity:
            raise SystemExit("resume input/model/settings do not match; use a new output directory")
    elif raw_exists:
        raise SystemExit("output already contains transcription; use --resume or a new directory")
    identity_path.write_text(json.dumps(identity, sort_keys=True, indent=2) + "\n")
    (outdir / "chunks").mkdir(exist_ok=True)
    (outdir / "raw").mkdir(exist_ok=True)
    (outdir / "chunks" / "plan.json").write_text(json.dumps({"source_start": args.start, "source_end": requested_end, "chunk_seconds": chunk_seconds, "chunks": [{"id": i + 1, "start": a, "end": b} for i, (a, b) in enumerate(chunks)]}, indent=2) + "\n", encoding="utf-8")
    all_segments = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="local-asr-", dir=outdir) as td:
        for index, (chunk_start, chunk_end) in enumerate(chunks, 1):
            raw_base = outdir / "raw" / f"chunk-{index:04d}"
            raw_json = raw_base.with_suffix(".json")
            if not (args.resume and raw_json.exists()):
                wav = Path(td) / f"chunk-{index:04d}.wav"
                extract_audio(ffmpeg, media, wav, chunk_start, chunk_end - chunk_start)
                cmd = [whisper, "--model", str(model), "--file", str(wav), "--language", args.language, "--output-json", "--output-file", str(raw_base), "--no-prints"]
                if args.translate: cmd.append("--translate")
                if args.no_gpu: cmd.append("--no-gpu")
                if args.threads: cmd += ["--threads", str(args.threads)]
                result = run(cmd)
                if result.returncode:
                    raise SystemExit(result.stderr.strip() or f"whisper-cli failed on chunk {index}")
            all_segments.extend(parse_whisper_json(raw_json, chunk_start, f"c{index:04d}"))
    write_outputs(outdir, media, p, all_segments, outdir / "raw" / "chunk-0001.json", {"engine": "whisper.cpp", "model": str(model), "model_sha256": sha256(model), "language": args.language, "source_start_sec": args.start, "source_duration_sec": args.duration, "chunk_seconds": chunk_seconds, "resume": args.resume, "no_gpu": args.no_gpu, "translate": args.translate, "threads": args.threads}, time.monotonic() - started)
    print(outdir)
    return 0


def main() -> int:
    # 运行时与模型路径是公共参数，挂在各子命令下，写法统一为：
    #   local_asr.py transcribe in.mp4 --out DIR --model MODEL
    #   local_asr.py doctor --model MODEL
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ffmpeg", default=os.getenv("LOCAL_ASR_FFMPEG"))
    common.add_argument("--ffprobe", default=os.getenv("LOCAL_ASR_FFPROBE"))
    common.add_argument("--whisper-cli", default=os.getenv("LOCAL_ASR_WHISPER_CLI"))
    common.add_argument("--model", default=os.getenv("LOCAL_ASR_MODEL"))

    parser = argparse.ArgumentParser(description="Local offline ASR wrapper for whisper.cpp")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", parents=[common]).set_defaults(func=doctor)

    t = sub.add_parser("transcribe", parents=[common])
    t.add_argument("media")
    t.add_argument("--out", required=True)
    t.add_argument("--language", default="auto")
    t.add_argument("--threads", type=int)
    t.add_argument("--start", type=float, default=0.0)
    t.add_argument("--duration", type=float)
    t.add_argument("--chunk-seconds", type=float,
                   help="split long media into independently resumable chunks")
    t.add_argument("--resume", action="store_true",
                   help="reuse completed raw chunk JSON files")
    t.add_argument("--translate", action="store_true")
    t.add_argument("--no-gpu", action="store_true",
                   help="disable Metal/GPU when the local runtime cannot allocate Metal buffers")
    t.set_defaults(func=transcribe)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
