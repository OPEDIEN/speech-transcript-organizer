# 本地转写

有可靠字幕或速记时先复用；没有文字才用此路线。只处理本地媒体，不上传音频、不调用收费 API、不自动下载模型。

先读 [前置检查](prerequisites.md)。工具与模型路径由参数或 `LOCAL_ASR_FFMPEG`、`LOCAL_ASR_FFPROBE`、`LOCAL_ASR_WHISPER_CLI`、`LOCAL_ASR_MODEL` 提供。

```bash
python3 <SKILL_DIR>/scripts/local_asr.py doctor --model <模型文件>
python3 <SKILL_DIR>/scripts/local_asr.py transcribe <视频> --out <新输出目录> --model <模型文件> --language auto --duration 15
```

用有人声的短段确认模型能加载、文字可用；全段另用新目录并去掉 `--duration`。`doctor` 是环境检查，不等于听写成功。GPU 失败时可加 `--no-gpu` 显式走 CPU。

## 输出和恢复

- `原文.md`：纯正文，作为后续整理基准，不混入时间和诊断。
- `带时间戳.md`、`segments.json`：原视频绝对秒数；每段唯一编号，`source_span` 指向原文字符范围。
- `asr-quality.json`：引擎与结构状态，不代表听写正确。
- `manifest.json`、`raw/`：媒体、模型、运行设置和原始结果，供追查。

`--start` / `--duration` 处理局部但保留原视频绝对时间。`--chunk-seconds` 为长任务按需分块，跨块处要检查漏字和重复。`--resume` 只在输入、模型和影响转写的设置一致时复用，不能复用另一段视频的同名文件。

回查专名、数字、静音/配乐和块边界，不能看到一段常见字幕套话就自动删除。没有可用回听工具时明示检查范围。保留原转写，修订另存；不要把缺少语言信息或没有音轨当成模型识别完成。
