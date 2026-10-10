#!/usr/bin/env python3
"""Task-scoped, local-only dependency checks. No installation or download.

    preflight.py --out-dir DIR                              # 只查 Python / 目录 / 磁盘
    preflight.py --media talk.mp4 --video --out-dir DIR      # 单文件：查画面
    preflight.py --media talk.mp4 --asr --model m.bin --out-dir DIR
    preflight.py --video-file video.mp4 --audio-file audio.mp3 \
                 --video --asr --model m.bin --out-dir DIR   # 音视频分离

音视频分离是常态（YouTube 高清走 DASH 分流、部分录屏工具也这样）：
`--video-file` / `--audio-file` 分别指定，只给 `--media` 时按单文件处理。
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def probe_media(ffprobe, path, timeout=30):
    """返回 (ok, kinds, detail)。kinds 是媒体里实际有的流类型集合。"""
    try:
        p = subprocess.run(
            [ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=timeout)
        if p.returncode:
            return False, set(), p.stderr[:300] or 'ffprobe failed'
        data = json.loads(p.stdout)
        kinds = {x.get('codec_type') for x in data.get('streams', [])}
        return True, kinds, 'streams: ' + ', '.join(sorted(k for k in kinds if k))
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return False, set(), str(e)


def decode_sample(ffmpeg, path, stream, opts, timeout=30):
    """真解码一小段——只查二进制存在是不够的。"""
    try:
        q = subprocess.run(
            [ffmpeg, '-v', 'error', '-i', str(path), '-map', stream, *opts,
             '-f', 'null', '-'],
            capture_output=True, text=True, timeout=timeout)
        return q.returncode == 0, (q.stderr[:300] or 'short sample decoded')
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def check(args):
    checks = []

    def record(name, ok, detail):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    # 音视频分离时两个文件各自独立；只给 --media 时两者都指向它
    video_file = args.video_file or args.media
    audio_file = args.audio_file or args.media

    record('python', sys.version_info >= (3, 10), sys.executable + ' ' + sys.version.split()[0])

    try:
        out = Path(args.out_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=out) as f:
            f.write(b'check')
            f.flush()
        record('output', True, str(out.resolve()))
        free = shutil.disk_usage(out).free
        record('free space', free > 0,
               str(free) + ' bytes available; compare with planned audio/frames/model size')
    except OSError as e:
        record('output', False, str(e))

    paths = {}
    needs_media = bool(args.media or args.video_file or args.audio_file or args.video or args.asr)
    needed = ['ffmpeg', 'ffprobe'] if needs_media else []
    if args.asr:
        needed.append('whisper-cli')
    for name in needed:
        override = getattr(args, name.replace('-', '_'), None)
        path = str(Path(override).expanduser()) if override else shutil.which(name)
        if not path:
            record(name, False, 'not found; provide explicit tool path or install for this route')
            continue
        # 超时给足：本机实测 whisper-cli 冷启动 + 机器被别的重任务占满时，
        # 一次 --version 要 20 秒以上（8 GiB 机器跑 ASR 时很容易撞上）。
        # 15 秒会把"机器忙"误报成"工具坏了"。
        try:
            p = subprocess.run([path, '--help' if name == 'whisper-cli' else '-version'],
                               capture_output=True, text=True, timeout=args.tool_timeout)
            record(name, p.returncode == 0, path + ': ' + (p.stdout or p.stderr)[:200])
            if p.returncode == 0:
                paths[name] = path
        except (OSError, subprocess.TimeoutExpired) as e:
            record(name, False, str(e))

    if args.asr:
        model = Path(args.model).expanduser() if args.model else None
        try:
            if not model or not model.is_file() or model.stat().st_size == 0:
                raise ValueError('provide a non-empty compatible local model with --model')
            with model.open('rb') as f:
                f.read(1)
            record('model', True,
                   str(model) + '; model loading still requires a real transcription sample')
        except (OSError, ValueError) as e:
            record('model', False, str(e))

    # 画面：要抽页就查
    if args.video:
        if not video_file:
            record('video supplied', False, 'pass --media or --video-file for the picture')
        else:
            vf = Path(video_file).expanduser()
            record('video file', vf.is_file(), str(vf))
            if vf.is_file() and 'ffprobe' in paths:
                ok, kinds, detail = probe_media(paths['ffprobe'], vf)
                record('video probe', ok and 'video' in kinds, detail)
                if ok and 'video' in kinds and 'ffmpeg' in paths:
                    good, why = decode_sample(paths['ffmpeg'], vf, '0:v:0', ['-frames:v', '1'])
                    record('video decode', good, why)

    # 声音：要转写就查
    if args.asr:
        if not audio_file:
            record('audio supplied', False, 'pass --media or --audio-file for the sound')
        else:
            af = Path(audio_file).expanduser()
            record('audio file', af.is_file(), str(af))
            if af.is_file() and 'ffprobe' in paths:
                ok, kinds, detail = probe_media(paths['ffprobe'], af)
                record('audio probe', ok and 'audio' in kinds, detail)
                if ok and 'audio' in kinds and 'ffmpeg' in paths:
                    good, why = decode_sample(paths['ffmpeg'], af, '0:a:0', ['-t', '1'])
                    record('audio decode', good, why)

    # 分离素材时，把"两条时间轴要对齐"这件事显式说出来
    if video_file and audio_file and Path(video_file).expanduser() != Path(audio_file).expanduser():
        record('split sources', True,
               'picture and sound come from two files; verify both cover the same time range')

    return {
        'ready': all(c['ok'] for c in checks),
        'checks': checks,
        'limits': 'Local runtime and sample decoding only; does not validate transcription, '
                  'page completeness, network access, or AI image/audio capabilities.',
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-dir', required=True)
    p.add_argument('--media', help='single file carrying both picture and sound')
    p.add_argument('--video-file', help='picture file when video and audio are separate')
    p.add_argument('--audio-file', help='sound file when video and audio are separate')
    p.add_argument('--video', action='store_true')
    p.add_argument('--asr', action='store_true')
    p.add_argument('--tool-timeout', type=float, default=60.0,
                   help='seconds to wait for a tool version check (default 60; '
                        'a busy machine can take >20s just to start whisper-cli)')
    p.add_argument('--model', default=os.getenv('LOCAL_ASR_MODEL'))
    for name in ('ffmpeg', 'ffprobe', 'whisper-cli'):
        p.add_argument('--' + name,
                       default=os.getenv('LOCAL_ASR_' + name.upper().replace('-', '_')))
    result = check(p.parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
