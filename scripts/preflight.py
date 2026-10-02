#!/usr/bin/env python3
"""Task-scoped, local-only dependency checks. No installation or download."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def check(args):
    checks = []
    def record(name, ok, detail):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
    record('python', sys.version_info >= (3, 10), sys.executable + ' ' + sys.version.split()[0])
    try:
        out = Path(args.out_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=out) as f:
            f.write(b'check'); f.flush()
        record('output', True, str(out.resolve()))
        free = shutil.disk_usage(out).free
        record('free space', free > 0, str(free) + ' bytes available; compare with planned audio/frames/model size')
    except OSError as e:
        record('output', False, str(e))
    paths = {}
    needed = ['ffmpeg', 'ffprobe'] if args.media or args.video or args.asr else []
    if args.asr: needed.append('whisper-cli')
    for name in needed:
        override = getattr(args, name.replace('-', '_'), None)
        path = str(Path(override).expanduser()) if override else shutil.which(name)
        if not path:
            record(name, False, 'not found; provide explicit tool path or install for this route')
            continue
        try:
            p = subprocess.run([path, '--help' if name == 'whisper-cli' else '-version'], capture_output=True, text=True, timeout=15)
            record(name, p.returncode == 0, path + ': ' + (p.stdout or p.stderr)[:200])
            if p.returncode == 0: paths[name] = path
        except (OSError, subprocess.TimeoutExpired) as e:
            record(name, False, str(e))
    if args.asr:
        model = Path(args.model).expanduser() if args.model else None
        try:
            if not model or not model.is_file() or model.stat().st_size == 0:
                raise ValueError('provide a non-empty compatible local model with --model')
            with model.open('rb') as f: f.read(1)
            record('model', True, str(model) + '; model loading still requires a real transcription sample')
        except (OSError, ValueError) as e:
            record('model', False, str(e))
    if args.video or args.asr:
        record('media supplied', bool(args.media), 'required for real decoding check')
    if args.media:
        media = Path(args.media).expanduser()
        record('input file', media.is_file(), str(media))
        if media.is_file() and 'ffprobe' in paths:
            try:
                p = subprocess.run([paths['ffprobe'], '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(media)], capture_output=True, text=True, timeout=30)
                if p.returncode: raise ValueError(p.stderr[:300])
                data = json.loads(p.stdout)
                kinds = {x.get('codec_type') for x in data.get('streams', [])}
                record('media probe', bool(kinds), 'streams: ' + ', '.join(sorted(kinds)))
                wanted = []
                if args.video: wanted.append(('video', '0:v:0', ['-frames:v', '1']))
                if args.asr: wanted.append(('audio', '0:a:0', ['-t', '1']))
                if not wanted: wanted = [(k, '0:v:0' if k == 'video' else '0:a:0', ['-t', '1']) for k in ('video', 'audio') if k in kinds]
                for kind, stream, opts in wanted:
                    if kind not in kinds:
                        record(kind + ' decode', False, 'required stream missing'); continue
                    if 'ffmpeg' not in paths: continue
                    q = subprocess.run([paths['ffmpeg'], '-v', 'error', '-i', str(media), '-map', stream, *opts, '-f', 'null', '-'], capture_output=True, text=True, timeout=30)
                    record(kind + ' decode', q.returncode == 0, q.stderr[:300] or 'short sample decoded')
            except (ValueError, OSError, subprocess.TimeoutExpired) as e:
                record('media decoding', False, str(e))
    return {'ready': all(c['ok'] for c in checks), 'checks': checks,
            'limits': 'Local runtime and sample decoding only; does not validate transcription, page completeness, network access, or AI image/audio capabilities.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out-dir', required=True)
    p.add_argument('--media')
    p.add_argument('--video', action='store_true')
    p.add_argument('--asr', action='store_true')
    p.add_argument('--model', default=os.getenv('LOCAL_ASR_MODEL'))
    for name in ('ffmpeg', 'ffprobe', 'whisper-cli'):
        p.add_argument('--' + name, default=os.getenv('LOCAL_ASR_' + name.upper().replace('-', '_')))
    result = check(p.parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ready'] else 1

if __name__ == '__main__': raise SystemExit(main())
