import re
import asyncio
import random
import os
import sys
from pydub import AudioSegment
import edge_tts
from tqdm import tqdm


MD_FILE = r"D:\Notion\雅思语法核心词汇.md"



# ==================== FFmpeg ====================
ffmpeg_bin_dir = "FFmpeg/bin"
os.environ["PATH"] = ffmpeg_bin_dir + os.pathsep + os.environ.get("PATH", "")
ffmpeg_exe = os.path.join(ffmpeg_bin_dir, "ffmpeg.exe")
ffprobe_exe = os.path.join(ffmpeg_bin_dir, "ffprobe.exe")
if not os.path.exists(ffmpeg_exe) or not os.path.exists(ffprobe_exe):
    print("❌ FFmpeg不存在")
    sys.exit(1)
AudioSegment.converter = ffmpeg_exe
AudioSegment.ffprobe = ffprobe_exe
# ==================== 参数 ====================
OUT = os.path.abspath("output_audio")
TMP_DIR = os.path.abspath("tmp")
EN_VOICE = "en-US-AriaNeural"
ZH_VOICE = "zh-CN-XiaoxiaoNeural"
EN_REPEAT_COUNT = 2
MAX_CONCURRENT_REQUESTS = 8
semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)


# ==================== Markdown解析 ====================
def parse_md(file):
    unit_dict = {}
    current_unit = "未命名单元"
    with open(file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 一级标题
            if line.startswith("# ") and not line.startswith("##"):
                unit_name = line[2:].strip()
                unit_name = re.sub(r'[\\/:*?"<>|]', "", unit_name).strip()
                current_unit = unit_name
                unit_dict.setdefault(current_unit, [])
                continue
            # 忽略二三级标题
            if line.startswith("##"):
                continue
            # 去编号
            line = re.sub(r"^\s*\d+[\.\s、]*", "", line)
            # 最后中文括号
            m = re.search(r"[（(]([^)）]+)[)）]\s*$", line)
            if not m:
                continue
            zh = m.group(1).strip()
            en = line[:m.start()].strip()
            en = en.replace("**", "")
            # 处理 confident(e)
            idx = en.find("(")
            if idx > 0 and en[idx - 1] != " ":
                end = en.find(")", idx)
                if end > 0:
                    base = en[:idx]
                    suffix = en[idx + 1:end]
                    en = f"{base}, {base}{suffix}"
            en = re.sub(r"\s+", " ", en)
            if en and zh:
                unit_dict.setdefault(current_unit, [])
                unit_dict[current_unit].append({
                    "en": en, "zh": zh
                })
    total = sum(len(v)
                for v in unit_dict.values())
    print(f"✅解析 {len(unit_dict)} 单元，共 {total} 词")
    return unit_dict


# ==================== TTS ====================
async def save_tts(text, voice, file):
    async with semaphore:
        for i in range(3):
            try:
                await edge_tts.Communicate(text, voice).save(file)
                if os.path.exists(file) and os.path.getsize(file) > 1024:
                    return True
            except Exception:
                await asyncio.sleep(0.5)
    return False


# ==================== 单词下载 ====================
async def download_word(prefix, index, w, mode, pbar):
    en_file = os.path.join(TMP_DIR, f"{prefix}_{index}_en.mp3")
    zh_file = os.path.join(TMP_DIR, f"{prefix}_{index}_zh.mp3")
    if mode == "en":
        await save_tts(w["en"], EN_VOICE, en_file)
        result = (en_file, None)
    elif mode == "zh":
        await save_tts(w["zh"], ZH_VOICE, zh_file)
        result = (None, zh_file)
    else:
        await asyncio.gather(save_tts(w["en"], EN_VOICE, en_file), save_tts(w["zh"], ZH_VOICE, zh_file))
        result = (en_file, zh_file)
    pbar.update(1)
    return result


# ==================== 音频合并 ====================
async def make_audio(words, mode, outfile, prefix):
    os.makedirs(TMP_DIR, exist_ok=True)
    outfile = os.path.abspath(outfile)
    os.makedirs(os.path.dirname(outfile), exist_ok=True)
    pbar = tqdm(total=len(words), desc=os.path.basename(outfile))
    tasks = [
        download_word(prefix, i, w, mode, pbar)
        for i, w in enumerate(words)
    ]
    files = await asyncio.gather(*tasks)
    pbar.close()
    print("🔗开始合并...")
    final_audio = AudioSegment.empty()
    silence400 = AudioSegment.silent(400)
    silence800 = AudioSegment.silent(800)
    for f_en, f_zh in files:
        en_audio = None
        zh_audio = None
        try:
            if f_en and os.path.exists(f_en) and os.path.getsize(f_en) > 3000:
                en_audio = AudioSegment.from_mp3(f_en)
        except:
            pass
        try:
            if f_zh and os.path.exists(f_zh) and os.path.getsize(f_zh) > 3000:
                zh_audio = AudioSegment.from_mp3(f_zh)
        except:
            pass
        if en_audio:
            for i in range(EN_REPEAT_COUNT):
                final_audio += en_audio
                if i < EN_REPEAT_COUNT - 1:
                    final_audio += silence400
        if zh_audio:
            if en_audio:
                final_audio += silence400
            final_audio += zh_audio
        if en_audio or zh_audio:
            final_audio += silence800
        # 删除临时文件
        # for f in (f_en, f_zh):
        #     if f and os.path.exists(f):
        #         try:
        #             os.remove(f)
        #         except:
        #             pass
    if len(final_audio) == 0:
        print("❌没有有效音频")
        return

    out_dir = os.path.dirname(outfile)

    if not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    print("💾导出:", outfile)
    final_audio.export(outfile, format="mp3", bitrate="128k", parameters=[
        "-threads", "4"
    ])
    print("✅完成:", outfile)


async def make_random_from_tmp(unit, unit_dir, outname, mode):
    files = []

    for f in os.listdir(TMP_DIR):
        if f.startswith(unit):
            files.append(f)

    if not files:
        print("❌ tmp没有找到:", unit)
        return

    random.shuffle(files)

    audio = AudioSegment.empty()

    silence400 = AudioSegment.silent(400)
    silence800 = AudioSegment.silent(800)

    for name in files:
        path = os.path.join(TMP_DIR, name)

        try:
            if mode == "en":
                if "_en.mp3" not in name:
                    continue

                seg = AudioSegment.from_mp3(path)
                audio += seg
                audio += silence800

            else:
                if "_en.mp3" in name:
                    en = AudioSegment.from_mp3(path)
                    audio += en
                    audio += silence400

                    zh_path = path.replace(
                        "_en.mp3",
                        "_zh.mp3"
                    )

                    if os.path.exists(zh_path):
                        zh = AudioSegment.from_mp3(zh_path)
                        audio += zh
                        audio += silence800

        except Exception as e:
            print("跳过:", name, e)

    outfile = os.path.join(
        unit_dir,
        outname
    )

    if len(audio):
        audio.export(
            outfile,
            format="mp3",
            bitrate="128k"
        )

        print("✅随机生成:", outfile)


# ==================== 主程序 ====================
async def main():
    os.makedirs(OUT, exist_ok=True)
    data = parse_md(MD_FILE)
    for unit, words in data.items():
        print(f"\n📂 {unit}")
        unit_dir = os.path.join(OUT, unit)
        os.makedirs(unit_dir, exist_ok=True)

        files = [
            "01_order_en_zh.mp3",
            "02_order_en.mp3",
            "03_random_en.mp3",
            "04_random_en_zh.mp3"
        ]

        if all(os.path.exists(os.path.join(unit_dir, f)) for f in files):
            print("⏭️ 已存在，跳过:", unit)
            continue

        # 测试时改 words[:15]
        test_words = words
        await make_audio(test_words, "mix", os.path.join(unit_dir, "01_order_en_zh.mp3"), unit + "_mix")
        await make_audio(test_words, "en", os.path.join(unit_dir, "02_order_en.mp3"), unit + "_en")

        random_en_words = words.copy()
        random.shuffle(random_en_words)
        await make_audio(random_en_words, "en",
                         os.path.join(unit_dir, "03_random_en.mp3"), unit + "_random_en")

        random_mix_words = words.copy()
        random.shuffle(random_mix_words)
        await make_audio(random_mix_words, "mix",
                         os.path.join(unit_dir, "04_random_en_zh.mp3"), unit + "_random_mix")

    print("\n🎉全部完成")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("停止")
