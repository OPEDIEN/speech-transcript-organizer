#!/usr/bin/env python3
"""扫描整条音轨，找出「不是正常演讲」的时间段 —— 演示视频、配乐、静音、掌声。

`references/video-segments.md` 要求对可疑段做**两个互相印证的证据**：
① 音频包络的停顿结构 ② 转写稿在同一时间窗有没有文本。
本脚本把这两件事都做掉，并直接给出候选段，免得在几十分钟的视频上手工逐段听。

判据（与 video-segments.md 一致）：

    能量正常起伏、有清晰句间停顿  → 人声
    长时间恒定能量、无任何停顿    → 配乐 / 会场底噪 / 演示视频
    长时间低于静音门限            → 静音

只在本地读音频，不写任何中间文件，不上传。只用标准库。

用法：
    python3 scan_segments.py <media> [--segments segments.json] [--out report.json]
"""
from __future__ import annotations

import argparse
import array
import json
import os
import re
import shutil
import subprocess
import sys

RMS_RE = re.compile(r"^lavfi\.astats\.Overall\.RMS_level=(.+)$")
PTS_RE = re.compile(r"^frame:.*pts_time:([0-9.]+)")


def require(name: str, override: str | None = None) -> str:
    path = str(os.path.expanduser(override)) if override else shutil.which(name)
    if not path:
        raise SystemExit(f"{name} not found; pass --{name.replace('-', '_')} or install it")
    return path


def envelope(ffmpeg: str, media: str) -> list[tuple[float, float]]:
    """逐帧 RMS（dBFS）。帧长约 21ms —— 比 0.1s 更细，才留得住句间停顿。

    用 astats 而不是 ebur128：ebur128 的瞬时响度是 400ms 滑窗，会把短停顿抹平。
    """
    cmd = [ffmpeg, "-v", "error", "-i", media, "-map", "0:a:0", "-vn",
           "-af", "astats=metadata=1:reset=1,"
                  "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
           "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    frames: list[tuple[float, float]] = []
    t = 0.0
    for line in proc.stdout:
        line = line.strip()
        m = PTS_RE.match(line)
        if m:
            t = float(m.group(1))
            continue
        m = RMS_RE.match(line)
        if m:
            raw = m.group(1)
            db = -120.0 if raw in ("-inf", "inf", "nan") else float(raw)
            frames.append((t, db))
    err = proc.stderr.read()
    proc.wait()
    if proc.returncode:
        raise SystemExit(err.strip() or "ffmpeg failed to decode audio")
    if not frames:
        raise SystemExit("no audio frames decoded; does the media have an audio track?")
    return frames


def runs(frames: list[tuple[float, float]], pred) -> list[tuple[float, float]]:
    """把满足 pred 的连续帧并成 [起, 止) 区间。"""
    out, start = [], None
    for i, (t, db) in enumerate(frames):
        if pred(db):
            if start is None:
                start = t
        elif start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, frames[-1][0]))
    return out


def stats_for(frames, a, b):
    """区间内的能量画像。音乐/底噪动态范围窄，人声宽 —— 作为停顿结构之外的旁证。"""
    dbs = sorted(db for t, db in frames if a <= t < b)
    if not dbs:
        return {}
    pick = lambda q: dbs[min(len(dbs) - 1, int(q * len(dbs)))]
    return {"db_p5": round(pick(0.05), 1), "db_median": round(pick(0.5), 1),
            "db_p95": round(pick(0.95), 1), "db_spread": round(pick(0.95) - pick(0.05), 1)}


def classify(frames, silence_db, pause_min, min_sustained, min_silence):
    """按「能量 + 停顿结构」切段。

    关键是**先按停顿切开，再找无停顿的长段**。
    不能把「极大非静音区间」整个拿来判断：演示视频常与它前后的演讲之间没有静音，
    一旦并成一段，演讲那边的句间停顿就会把整段判成人声，从而漏掉演示段。
    """
    t0, t1 = frames[0][0], frames[-1][0]
    sil = runs(frames, lambda db: db < silence_db)
    pauses = [(a, b) for a, b in sil if b - a >= pause_min]

    # 被停顿切开后，仍然「一口气没有停顿」的长段 → 配乐/底噪/演示视频
    sustained = []
    cursor = t0
    for a, b in pauses:
        if a - cursor >= min_sustained:
            sustained.append((cursor, a))
        cursor = max(cursor, b)
    if t1 - cursor >= min_sustained:
        sustained.append((cursor, t1))

    # 剩下的就是人声。报告静音段时只算 ≥ min_silence 的：
    # 词间那些 0.1–0.3s 的小停顿属于正常说话，算进静音会让占比虚高。
    sil_long = [(a, b) for a, b in sil if b - a >= min_silence]
    claimed = sorted(sustained + sil_long)
    speech, cursor = [], t0
    for a, b in claimed:
        if a > cursor:
            speech.append((cursor, a))
        cursor = max(cursor, b)
    if t1 > cursor:
        speech.append((cursor, t1))

    regions = []
    for kind, spans in (("speech", speech), ("sustained", sustained), ("silence", sil_long)):
        for start, end in spans:
            r = {"kind": kind, "start": round(start, 3), "end": round(end, 3),
                 "duration": round(end - start, 3)}
            r.update(stats_for(frames, start, end))
            regions.append(r)
    regions.sort(key=lambda r: r["start"])
    return regions


def attach_asr(regions, segments_path):
    """证据 B：每个候选段里到底有没有转写文本。"""
    if not segments_path:
        return None
    with open(segments_path, encoding="utf-8") as f:
        segs = json.load(f)["segments"]
    for r in regions:
        inside = [s for s in segs
                  if s["start"] < r["end"] and s["end"] > r["start"]]
        r["asr_segments"] = len(inside)
        r["asr_chars"] = sum(len(s["text"]) for s in inside)
        r["asr_sample"] = "".join(s["text"] for s in inside)[:40]
    return segs


def findings(regions, min_sustained, min_missing):
    out = []
    for r in regions:
        has_asr = r.get("asr_segments") is not None
        if r["kind"] == "sustained" and has_asr and r["asr_segments"]:
            out.append("⚠ %.1f–%.1fs（%.0fs）连续能量段里却有 %d 段转写文本：%r\n"
                       "     可能是人声叠在掌声/配乐上（文本为真），也可能是 ASR 在纯配乐上的幻觉。\n"
                       "     能量统计不足以区分两者，必须回听确认。"
                       % (r["start"], r["end"], r["duration"], r["asr_segments"], r["asr_sample"]))
        elif r["kind"] == "sustained":
            spread = r.get("db_spread")
            if spread is not None and spread > 35:
                out.append("⚠ %.1f–%.1fs（%.0fs）无句间停顿，但动态范围 %.0f dB 偏大 —— "
                           "可能是长时间连续说话而非配乐，需回听"
                           % (r["start"], r["end"], r["duration"], spread))
            else:
                out.append("· %.1f–%.1fs（%.0fs）恒定能量、无句间停顿（动态范围 %.0f dB）"
                           "→ 候选，需结合画面与回听确认"
                           % (r["start"], r["end"], r["duration"], spread if spread is not None else -1))
        elif r["kind"] == "speech" and has_asr and not r["asr_segments"] and r["duration"] >= min_missing:
            out.append("⚠ %.1f–%.1fs（%.0fs）有波动能量但转写稿无文本 —— 疑似漏转写，需回听"
                       % (r["start"], r["end"], r["duration"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描音轨，找出演示段/配乐/静音候选")
    ap.add_argument("media")
    ap.add_argument("--segments", help="segments.json，用于核对候选段有没有转写文本")
    ap.add_argument("--out", help="把结构化结果写到这个 JSON")
    ap.add_argument("--ffmpeg", default=os.getenv("LOCAL_ASR_FFMPEG"))
    ap.add_argument("--silence-db", type=float, default=-50.0, help="静音门限（dBFS，默认 -50）")
    ap.add_argument("--pause-min", type=float, default=0.25, help="多长的下降算句间停顿（秒，默认 0.25）")
    ap.add_argument("--min-sustained", type=float, default=20.0,
                    help="多长的无停顿段算持续音频（秒，默认 20）")
    ap.add_argument("--min-silence", type=float, default=1.0,
                    help="多长的低能量段才作为「静音段」报告（秒，默认 1.0；更短的是词间停顿）")
    ap.add_argument("--min-missing", type=float, default=10.0,
                    help="多长的人声段没文本才报漏转写（秒，默认 10）")
    args = ap.parse_args()

    if any(v <= 0 for v in (args.pause_min, args.min_sustained, args.min_silence, args.min_missing)):
        ap.error("duration thresholds must be positive")
    if not os.path.isfile(args.media):
        raise SystemExit(f"media not found: {args.media}")
    ffmpeg = require("ffmpeg", args.ffmpeg)

    frames = envelope(ffmpeg, args.media)
    regions = classify(frames, args.silence_db, args.pause_min, args.min_sustained, args.min_silence)
    attach_asr(regions, args.segments)
    notes = findings(regions, args.min_sustained, args.min_missing)

    speech = sum(r["duration"] for r in regions if r["kind"] == "speech")
    sustained = sum(r["duration"] for r in regions if r["kind"] == "sustained")
    silence = sum(r["duration"] for r in regions if r["kind"] == "silence")
    total = frames[-1][0] or 1.0

    print("音轨扫描：%s" % args.media)
    print("  帧数 %d（约 %.0f ms/帧）  覆盖 %.1fs" % (len(frames), 1000 * total / len(frames), total))
    print("  人声 %.1fs（%.0f%%）｜ 恒定能量 %.1fs（%.0f%%）｜ 静音 %.1fs（%.0f%%）"
          % (speech, 100 * speech / total, sustained, 100 * sustained / total,
             silence, 100 * silence / total))
    print()
    if notes:
        print("发现：")
        for n in notes:
            print("  " + n)
    else:
        print("发现：此规则未发现候选，不代表转写完整或准确")

    if args.out:
        payload = {"schema_version": 1, "source_media": os.path.abspath(args.media),
                   "covered_sec": round(total, 3),
                   "params": {"silence_db": args.silence_db, "pause_min_sec": args.pause_min,
                              "min_sustained_sec": args.min_sustained},
                   "regions": regions, "findings": notes}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("\n已写出：%s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
