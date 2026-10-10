#!/usr/bin/env python3
"""扫描整条音轨，找出「不是正常演讲」的时间段 —— 演示视频、配乐、静音、掌声。

`references/video-segments.md` 要求对可疑段做**两个互相印证的证据**：
① 音频包络的停顿结构 ② 转写稿在同一时间窗有没有文本。
本脚本把这两件事都做掉，并直接给出候选段，免得在几十分钟的视频上手工逐段听。

判据（与 video-segments.md 一致）：

    能量正常起伏、有清晰句间停顿  → 人声
    长时间恒定能量、无任何停顿    → 配乐 / 会场底噪 / 演示视频
    长时间低于静音门限            → 静音

判据里那个静音门限是**绝对 dB，换录音不一定合适**：音频被压过的直播流上，句间停顿
可能整片落不到门限以下，正常演讲会被整段误判成「恒定能量」。实测**任何全局门限都分不开**
（某直播流的语音停顿只落到 −45 dB，而另一片素材的配乐谷底是 −46 dB，可用窗口不到 1 dB）。
所以脚本不假装能分开，而是**自检「本片判据到底可不可用」**（人声段 vs 恒定能量段的
动态范围之比，需 ≥1.5×），不可靠时整体降级、只报一条说明 + 一行待判断段落清单。

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


def calibrate(regions):
    """本片「停顿结构」判据到底可不可用 —— 用人声段与恒定能量段的动态范围之比自检。

    区分度够（人声 spread 明显宽于恒定能量 spread）时，逐段结论可信；
    不够时（压缩过的直播流常见）**任何阈值都分不开**，只能整体降级、请人工判断，
    而不是硬报一堆可疑段。
    """
    sp = sorted(r["db_spread"] for r in regions
                if r["kind"] == "speech" and r.get("db_spread") is not None)
    su = sorted(r["db_spread"] for r in regions
                if r["kind"] == "sustained" and r.get("db_spread") is not None)
    med = lambda xs: (xs[len(xs) // 2] if xs else None)
    S, U = med(sp), med(su)
    if S is None or U is None or U <= 0:
        return {"speech_spread_median": S, "sustained_spread_median": U,
                "ratio": None, "reliable": True}
    ratio = S / U
    return {"speech_spread_median": S, "sustained_spread_median": U,
            "ratio": round(ratio, 2), "reliable": ratio >= 1.5}


def merge_adjacent(spans, gap):
    """把中间只隔一小会儿（< gap 秒）的相邻区间并成一段。

    同一段配乐/演示里常夹着零点几秒的伪停顿，会被切成好几段连报
    （实测一次扫描出现 591.8–622.0 / 622.4–645.4 / 646.0–688.4 三段，本就是一段）。
    """
    if not spans:
        return []
    out = [list(spans[0])]
    for a, b in spans[1:]:
        if a - out[-1][1] < gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def classify(frames, silence_db, pause_min, min_sustained, min_silence, merge_gap=2.0):
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
    sustained = merge_adjacent(sustained, merge_gap)

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
                  if r["start"] <= (s["start"] + s["end"]) / 2 < r["end"]]
        r["asr_segments"] = len(inside)
        r["asr_chars"] = sum(len(s["text"]) for s in inside)
        r["asr_sample"] = "".join(s["text"] for s in inside)[:40]
    return segs


def findings(regions, min_sustained, min_missing, cal=None):
    """把候选段翻成人看的结论。

    `cal` 是 `calibrate()` 的结果。判据不可靠时**不再逐段报可疑**——那种录音上
    正常演讲本来就会被切成「恒定能量」，逐条报警只会淹没真正需要看的段落。
    """
    cal = cal or {}
    reliable = cal.get("reliable", True)
    out = []
    sustained = [r for r in regions if r["kind"] == "sustained"]
    if sustained and not reliable:
        out.append(
            "⚠ Pause-structure test unreliable on this recording: median spread %s dB (speech) "
            "vs %s dB (steady energy), separation only %sx (need >= 1.5x). Common on compressed "
            "live streams — **no global silence threshold can separate them**. The %d stretches "
            "below have no inter-sentence pauses; judge them by hand rather than assuming music "
            "or ASR hallucination."
            % (cal.get("speech_spread_median"), cal.get("sustained_spread_median"),
               cal.get("ratio"), len(sustained)))
        listed = ", ".join("%.0f–%.0fs" % (r["start"], r["end"]) for r in sustained[:12])
        if len(sustained) > 12:
            listed += " ... (%d total)" % len(sustained)
        out.append("   · stretches to judge by hand: " + listed)
    for r in regions:
        has_asr = r.get("asr_segments") is not None
        if r["kind"] == "sustained":
            if not reliable:
                continue          # 已在上面用一条总说明 + 一行清单交代，不再逐段刷屏
            spread = r.get("db_spread")
            # 动态范围窄 → 像配乐/底噪；宽 → 像连续说话。阈值 35 dB 只在判据可靠时有意义。
            music_like = spread is None or spread <= 35
            if music_like and has_asr and r["asr_segments"]:
                out.append("⚠ %.1f–%.1fs (%.0fs) steady energy but %d transcript segment(s): %r\n"
                           "     Could be speech over applause/music (the text is real), or ASR\n"
                           "     hallucination over music alone. The two are indistinguishable from\n"
                           "     audio (both measure ~20 dB spread); listen to confirm."
                           % (r["start"], r["end"], r["duration"], r["asr_segments"], r["asr_sample"]))
            elif music_like:
                out.append("· %.1f–%.1fs (%.0fs) steady energy, no inter-sentence pauses "
                           "(spread %.0f dB) -> music / room tone / demo video"
                           % (r["start"], r["end"], r["duration"], spread if spread is not None else -1))
            else:
                out.append("⚠ %.1f–%.1fs (%.0fs) no inter-sentence pauses, but spread %.0f dB is "
                           "wide — possibly continuous speech rather than music; listen to confirm"
                           % (r["start"], r["end"], r["duration"], spread))
        elif r["kind"] == "speech" and has_asr and not r["asr_segments"] and r["duration"] >= min_missing:
            out.append("⚠ %.1f–%.1fs (%.0fs) speech-like energy but no transcript text — "
                       "possibly missed transcription; listen to confirm"
                       % (r["start"], r["end"], r["duration"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Scan the audio track for demo segments, music and silence")
    ap.add_argument("media")
    ap.add_argument("--segments", help="segments.json, to check candidates against transcript text")
    ap.add_argument("--out", help="write the structured result to this JSON file")
    ap.add_argument("--ffmpeg", default=os.getenv("LOCAL_ASR_FFMPEG"))
    ap.add_argument("--silence-db", type=float, default=-50.0,
                    help="silence threshold (dBFS, default -50). **Absolute — may not fit another "
                         "recording**: on a compressed live stream it can fail across the whole "
                         "file, so the script also self-checks whether the test applies and degrades")
    ap.add_argument("--merge-gap", type=float, default=2.0,
                    help="merge adjacent steady-energy stretches separated by less than this (s)")
    ap.add_argument("--pause-min", type=float, default=0.25,
                    help="how long a dip must be to count as an inter-sentence pause (s)")
    ap.add_argument("--min-sustained", type=float, default=20.0,
                    help="how long a pause-free stretch must be to count as steady energy (s)")
    ap.add_argument("--min-silence", type=float, default=1.0,
                    help="how long a low-energy stretch must be to be reported as silence (s)")
    ap.add_argument("--min-missing", type=float, default=10.0,
                    help="how long a speech stretch without text must be to report a miss (s)")
    args = ap.parse_args()

    # 时长类阈值必须为正：0 或负数会让分类逻辑把整片判成一段，结论完全失真
    if any(v <= 0 for v in (args.pause_min, args.min_sustained,
                            args.min_silence, args.min_missing)):
        ap.error("duration thresholds must be positive")
    if not os.path.isfile(args.media):
        raise SystemExit(f"media not found: {args.media}")
    ffmpeg = require("ffmpeg", args.ffmpeg)

    frames = envelope(ffmpeg, args.media)
    silence_db = args.silence_db
    regions = classify(frames, silence_db, args.pause_min, args.min_sustained,
                       args.min_silence, args.merge_gap)
    attach_asr(regions, args.segments)
    cal = calibrate(regions)
    notes = findings(regions, args.min_sustained, args.min_missing, cal)

    speech = sum(r["duration"] for r in regions if r["kind"] == "speech")
    sustained = sum(r["duration"] for r in regions if r["kind"] == "sustained")
    silence = sum(r["duration"] for r in regions if r["kind"] == "silence")
    total = frames[-1][0] or 1.0

    def num(x, fmt="%.1f"):
        return "—" if x is None else fmt % x

    print("Audio scan: %s" % args.media)
    print("  %d frames (~%.0f ms/frame)  covering %.1fs"
          % (len(frames), 1000 * total / len(frames), total))
    print("  silence threshold %.1f dBFS (absolute; may not fit another recording)" % silence_db)
    print("  test self-check: speech spread median %s dB | steady energy %s dB | separation %sx -> %s"
          % (num(cal["speech_spread_median"]), num(cal["sustained_spread_median"]),
             num(cal["ratio"], "%.2f"),
             "reliable" if cal["reliable"] else "**UNRELIABLE, conclusions degraded**"))
    print("  speech %.1fs (%.0f%%) | steady energy %.1fs (%.0f%%) | silence %.1fs (%.0f%%)"
          % (speech, 100 * speech / total, sustained, 100 * sustained / total,
             silence, 100 * silence / total))
    print()
    if notes:
        print("Findings:")
        for n in notes:
            print("  " + n)
    else:
        # "没找到"不等于"没有问题"——扫描只看能量结构，看不到内容对不对
        print("Findings: this rule found no candidates — that does not mean the "
              "transcript is complete or accurate")

    if args.out:
        payload = {"schema_version": 2, "source_media": os.path.abspath(args.media),
                   "covered_sec": round(total, 3),
                   "params": {"silence_db": silence_db,
                              "pause_min_sec": args.pause_min,
                              "min_sustained_sec": args.min_sustained,
                              "min_silence_sec": args.min_silence,
                              "merge_gap_sec": args.merge_gap},
                   "calibration": cal,
                   "regions": regions, "findings": notes}
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("\nWrote: %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
