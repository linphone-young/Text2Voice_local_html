import os
import re
import sys
import uuid
import random
import asyncio
import shutil
import hashlib
import time
import atexit
import logging
from typing import List, Dict, Any
from contextlib import asynccontextmanager

import edge_tts
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ==================== 日志与基础配置 ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TTS_Engine")

BASE_DIR = os.path.abspath('.')
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "static", "output")
CACHE_DIR = os.path.join(BASE_DIR, "tmp_cache")

# 确保核心目录在启动时必须存在
for folder in [UPLOAD_DIR, OUTPUT_DIR, CACHE_DIR, os.path.join(BASE_DIR, "static")]:
    os.makedirs(folder, exist_ok=True)

# ==================== FFmpeg 智能跨系统环境适配 ====================
PROJECT_FFMPEG_DIR = os.path.join(BASE_DIR, "FFmpeg", "bin")
HARDCODED_BACKUP_DIR = "FFmpeg/bin"

chosen_ffmpeg_dir = None
if shutil.which("ffmpeg"):
    logger.info("🎉 完美！检测到当前 Windows 系统环境变量中已配置 FFmpeg。")
elif os.path.exists(PROJECT_FFMPEG_DIR):
    chosen_ffmpeg_dir = PROJECT_FFMPEG_DIR
    logger.info(f"📁 检测到项目内嵌 FFmpeg 目录: {chosen_ffmpeg_dir}")
elif os.path.exists(HARDCODED_BACKUP_DIR):
    chosen_ffmpeg_dir = HARDCODED_BACKUP_DIR
    logger.warning(f"⚠️ 未检测到系统 FFmpeg，激活本地备用路径: {chosen_ffmpeg_dir}")

if chosen_ffmpeg_dir:
    os.environ["PATH"] = chosen_ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

# 延迟导入 pydub，确保环境变量已经注入完毕
try:
    from pydub import AudioSegment
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, USLT

    if chosen_ffmpeg_dir:
        AudioSegment.converter = os.path.join(chosen_ffmpeg_dir, "ffmpeg.exe")
        AudioSegment.ffprobe = os.path.join(chosen_ffmpeg_dir, "ffprobe.exe")
except ImportError as e:
    logger.critical(f"💥 关键依赖导入失败，请检查是否运行了 pip install。错误: {e}")
    sys.exit(1)

# ==================== 安全防御：缓存并发写锁 ====================
_file_write_locks: Dict[str, asyncio.Lock] = {}


def get_file_lock(file_path: str) -> asyncio.Lock:
    if file_path not in _file_write_locks:
        _file_write_locks[file_path] = asyncio.Lock()
    return _file_write_locks[file_path]


# ==================== 临时文件清理器 ====================
def cleanup_all_tmp():
    """安全地清空历史成品，长期物理缓存 CACHE_DIR 选择保留以提升二次生成速度"""
    logger.info("🧹 正在执行系统级临时历史成品清理...")
    try:
        if os.path.exists(OUTPUT_DIR):
            for f in os.listdir(OUTPUT_DIR):
                fp = os.path.join(OUTPUT_DIR, f)
                if os.path.isfile(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass  # 忽略正在被读取的占用文件
        logger.info("✨ 历史垃圾清理完毕！")
    except Exception as e:
        logger.error(f"清理临时文件时遭遇非致命阻碍: {e}")


atexit.register(cleanup_all_tmp)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    cleanup_all_tmp()


app = FastAPI(title="智能加速音频生成器-开源版", lifespan=lifespan)

# 挂载前端静态组件
if os.path.exists(os.path.join(BASE_DIR, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# 支持扩展的固定音色池
EN_VOICES = {"en-US-AriaNeural": "Aria (美音-女)", "en-US-GuyNeural": "Guy (美音-男)",
             "en-GB-SoniaNeural": "Sonia (英音-女)", "en-GB-RyanNeural": "Ryan (英音-男)"}
ZH_VOICES = {"zh-CN-XiaoxiaoNeural": "晓晓 (国语-女)", "zh-CN-YunxiNeural": "云希 (国语-男)"}


# ==================== 高容错文本解析器 ====================
def parse_markdown_content(text: str) -> List[Dict[str, str]]:
    words = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-") or line.startswith("*"):
            continue
        # 剥离行首序号
        line = re.sub(r"^\s*\d+[\.\s、]*", "", line).strip()
        # 精准捕获行尾括号内的中文释义（全面兼容中英文括号）
        m = re.search(r"[（(]([^)）]+)[)）]\s*$", line)
        if m:
            zh = m.group(1).strip()
            en = line[:m.start()].strip()
        else:
            zh = ""
            en = line.strip()

        en = en.replace("**", "").strip()

        # 兼容高级短语组合变形，如 look(for) -> look, look for
        idx = en.find("(")
        if idx > 0 and en[idx - 1] != " ":
            end = en.find(")", idx)
            if end > 0:
                base = en[:idx]
                en = f"{base}, {base}{en[idx + 1:end]}"

        # 去除多余的空格
        en = re.sub(r"\s+", " ", en).strip()
        if en:
            words.append({"en": en, "zh": zh})

    logger.info(f"📝 文本结构解析成功：共捕获到 {len(words)} 组有效双语词条")
    return words


async def save_tts(text: str, voice: str, file_path: str) -> bool:
    """具备并发写锁与物理文件校验的防崩下载器"""
    if os.path.exists(file_path) and os.path.getsize(file_path) > 1000:
        return True

    lock = get_file_lock(file_path)
    async with lock:
        if os.path.exists(file_path) and os.path.getsize(file_path) > 1000:
            return True

        for attempt in range(2):
            try:
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(file_path)
                if os.path.exists(file_path) and os.path.getsize(file_path) > 1000:
                    return True
            except Exception as tts_err:
                logger.error(f"🌐 Edge-TTS 服务下载单片失败 (尝试 {attempt + 1}/2) -> 文本: '{text}', 原因: {tts_err}")
                await asyncio.sleep(0.3)
        return False


# ==================== 核心控制器 API ====================

@app.post("/api/clear_cache")
async def clear_cache():
    """手动清空物理缓存，应对用户需要全面强制刷新的场景"""
    cleanup_all_tmp()
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    return {"success": True, "message": "全局缓存及物理切片已全部强制销毁"}


@app.post("/api/generate")
async def generate_audio(
        file: UploadFile = File(...),
        en_repeat: int = Form(2),
        voice_mode: str = Form("default"),
        order_mode: str = Form("sequential"),
        mix_mode: str = Form("mix"),
        file_md5: str = Form("")
):
    start_total_time = time.time()

    try:
        content = (await file.read()).decode("utf-8")
    except Exception:
        raise HTTPException(status_code=400, detail="文件读取解密失败，请上传标准的 UTF-8 编码文本")

    words = parse_markdown_content(content)
    if not words:
        raise HTTPException(status_code=400, detail="未解析到有效词汇！请检查文件是否满足格式：'单词 (翻译)'")

    # 分配音色
    en_voice_key = list(EN_VOICES.keys())[0]
    zh_voice_key = list(ZH_VOICES.keys())[0]
    if voice_mode == "random":
        en_voice_key = random.choice(list(EN_VOICES.keys()))
        zh_voice_key = random.choice(list(ZH_VOICES.keys()))

    voice_info = f"{EN_VOICES[en_voice_key]} & {ZH_VOICES[zh_voice_key]}"

    # 并发控制：20 路高并发信号量
    start_download_time = time.time()
    semaphore = asyncio.Semaphore(20)

    async def download_pair(w: dict):
        async with semaphore:
            en_hash = hashlib.md5(f"{w['en']}_{en_voice_key}".encode('utf-8')).hexdigest()
            zh_hash = hashlib.md5(f"{w['zh']}_{zh_voice_key}".encode('utf-8')).hexdigest()

            en_path = os.path.join(CACHE_DIR, f"{en_hash}.mp3")
            zh_path = os.path.join(CACHE_DIR, f"{zh_hash}.mp3") if mix_mode == "mix" else None

            tasks = [save_tts(w["en"], en_voice_key, en_path)]
            if zh_path:
                tasks.append(save_tts(w["zh"], zh_voice_key, zh_path))

            results = await asyncio.gather(*tasks)
            final_en_path = en_path if results[0] else None
            final_zh_path = zh_path if (len(results) > 1 and results[1]) else None
            return final_en_path, final_zh_path

    # 执行全局调度
    download_tasks = [download_pair(w) for w in words]
    audio_files = await asyncio.gather(*download_tasks)
    end_download_time = time.time()

    # ✨ 优化：动态严格校验切片可用状态
    valid_en_count = sum(1 for f_en, _ in audio_files if f_en and os.path.exists(f_en))
    valid_zh_count = sum(1 for _, f_zh in audio_files if f_zh and os.path.exists(f_zh)) if mix_mode == "mix" else 1

    if valid_en_count == 0 or valid_zh_count == 0:
        raise HTTPException(
            status_code=500,
            detail="❗ 语音服务拦截：未能下载到完整的音频切片，请确认您的网络能正常访问微软 Edge 语音服务器，或尝试重新生成。"
        )

    avg_phrase_time = (end_download_time - start_download_time) / len(words)

    # 语序重排
    combined_data = list(zip(words, audio_files))
    if order_mode == "random":
        random.shuffle(combined_data)

    # 进入高可靠音频流拼装阶段
    start_stitch_time = time.time()
    final_audio = AudioSegment.empty()
    silence400 = AudioSegment.silent(duration=400)
    silence800 = AudioSegment.silent(duration=800)

    lrc_lines = []
    current_ms = 0

    for w, (f_en, f_zh) in combined_data:
        if not f_en or not os.path.exists(f_en):
            continue

        try:
            en_seg = AudioSegment.from_mp3(f_en)
            zh_seg = AudioSegment.from_mp3(f_zh) if f_zh and os.path.exists(f_zh) else None
        except Exception as pydub_err:
            logger.error(f"❌ 损坏的 MP3 切片，已自动跳过: {f_en}，错误: {pydub_err}")
            continue

        # 生成时间戳字幕行
        m, s = divmod(current_ms // 1000, 60)
        h_ms = (current_ms % 1000) // 10
        time_tag = f"[{m:02d}:{s:02d}.{h_ms:02d}]"

        if mix_mode == "mix":
            lrc_lines.append(f"{time_tag}{w['en']} ｜ {w['zh']}")
        else:
            lrc_lines.append(f"{time_tag}{w['en']}")

        # 拼接英文
        for r in range(en_repeat):
            final_audio += en_seg
            current_ms += len(en_seg)
            if r < en_repeat - 1:
                final_audio += silence400
                current_ms += 400

        # 拼接中文
        if zh_seg:
            final_audio += silence400
            current_ms += 400
            final_audio += zh_seg
            current_ms += len(zh_seg)

        final_audio += silence800
        current_ms += 800

    if len(final_audio) == 0:
        raise HTTPException(status_code=500, detail="拼装流水线崩溃：没有可导出的有效音频轨。")

    out_filename = f"{uuid.uuid4().hex}.mp3"
    out_path = os.path.join(OUTPUT_DIR, out_filename)

    # 异步线程池导出
    await asyncio.to_thread(lambda: final_audio.export(out_path, format="mp3", bitrate="128k"))
    end_stitch_time = time.time()

    # 内嵌同步歌词 (ID3v2 元数据封装)
    lrc_content = "\n".join(lrc_lines)
    try:
        audio_tags = MP3(out_path, ID3=ID3)
        try:
            audio_tags.add_tags()
        except Exception:
            pass
        audio_tags.tags.add(USLT(encoding=3, lang='eng', desc='Lyrics', text=lrc_content))
        audio_tags.save()
    except Exception as tag_err:
        logger.error(f"⚠️ 歌词元数据嵌入失败（不影响音频播放）: {tag_err}")

    end_total_time = time.time()

    return {
        "success": True,
        "audio_url": f"/static/output/{out_filename}",
        "lrc": lrc_content,
        "voice_info": voice_info,
        "total_words": len(lrc_lines),
        "time_stats": {
            "avg_phrase_time": round(avg_phrase_time, 3),
            "stitch_time": round(end_stitch_time - start_stitch_time, 3),
            "total_time": round(end_total_time - start_total_time, 3)
        }
    }


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    # 绑定 0.0.0.0 以实现多设备/跨端访问
    uvicorn.run("app:app", host="0.0.0.0", port=8003, reload=False)