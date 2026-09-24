import asyncio
import edge_tts
import os
import fitz
import subprocess
import re
import shutil
import json
import random
import pathlib
from tqdm import tqdm

os.environ["PYTHONUTF8"] = "1"

# =========================
# CONFIG
# =========================

VOICE_PRESETS = {

    "narrator": {
        "voice": "en-US-ChristopherNeural",
        "style": "serious",
        "rate": "+0%",
        "pitch": "+0Hz"
    },

    "documentary": {
        "voice": "en-US-BrianNeural",
        "style": "serious",
        "rate": "-4%",
        "pitch": "-1Hz"
    },

    "warm": {
        "voice": "en-US-GuyNeural",
        "style": "friendly",
        "rate": "-5%",
        "pitch": "+0Hz"
    },

    "story": {
        "voice": "en-GB-RyanNeural",
        "style": "narration-professional",
        "rate": "-10%",
        "pitch": "-2Hz"
    },

    "female_story": {
        "voice": "en-US-AriaNeural",
        "style": "narration-professional",
        "rate": "-5%",
        "pitch": "+0Hz"
    }
}

VOICE_PROFILE = "documentary"
AUDIO_FORMAT = "audio-24khz-96kbitrate-mono-mp3"
TRIM_END = float(4)
FIRST_CHUNK_TRIM_START = 26.8
NORMAL_TRIM_START = 27.2
CHAPTER_TRIM_START = 27
MAX_WORKERS = 6
RETRIES = 10
EDGE_COOLDOWN = 0.5
EDGE_CONCURRENCY = 3
DEFAULT_CHUNK_SIZE = 2500
CORRUPT_THRESHOLD = 1500


def normalize_english_text(text):
    """Normalize English text for clean TTS pronunciation.
    Handles PDF artifacts, ligatures, symbols, and spacing issues.
    """
    # --- Strip bullet points and list markers FIRST (before line joining) ---
    # Must run while markers are still at line starts
    # Unicode bullets: •‣⁃▪▫■□●○◆◇★☆➤►▸▹▾▿➔➜
    text = re.sub(r'(?m)^\s*[•‣⁃▪▫■□●○◆◇★☆➤►▸▹▾鿿➔➜]\s*', '', text)
    # Dash/asterisk bullets: "- " or "* " at line start
    text = re.sub(r'(?m)^\s*[-*]\s+', '', text)
    # Numbered lists: "1. ", "2) ", "(3) "
    text = re.sub(r'(?m)^\s*\(?\d+\)?[.)]\s+', '', text)
    # Lettered lists: "a. ", "b) ", "(c) "
    text = re.sub(r'(?m)^\s*\(?[a-zA-Z]\)?[.)]\s+', '', text)
    # Roman numeral lists: "I. ", "II) ", "(iii) "
    text = re.sub(
        r'(?m)^\s*\(?'                  # optional leading (
        r'(?:M{0,3})'                     # thousands
        r'(?:CM|CD|D?C{0,3})'             # hundreds
        r'(?:XC|XL|L?X{0,3})'             # tens
        r'(?:IX|IV|V?I{0,3})'             # ones
        r'\)?'                            # optional trailing )
        r'[.)]\s+',                       # dot or ) + space
        '', text, flags=re.IGNORECASE
    )

    # --- Fix PDF line-break artifacts ---
    # Hyphenation across lines: "hy-\nphen" → "hyphen"
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)
    # Word splits across lines: "word\nword" → "word word"
    text = re.sub(r'(\w)\s*\n\s*(\w)', r'\1 \2', text)

    # --- Normalize spaces ---
    text = re.sub(r'[ \t]+', ' ', text)

    # --- Remove URLs ---
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)

    # --- Remove standalone page numbers ---
    text = re.sub(r'(?m)^\s*\d+\s*$', ' ', text)
    text = re.sub(r'(?i)(?:page\s*)?\b\d+\b(?=\s*$)', '', text)

    # --- Remove control characters (except newline) ---
    text = re.sub(r'[\x00-\x09\x0B-\x1F\x7F]', ' ', text)

    # --- PDF ligature expansion ---
    text = text.replace('\ufb01', 'fi').replace('\ufb02', 'fl')
    text = text.replace('\ufb00', 'ff').replace('\ufb03', 'ffi').replace('\ufb04', 'ffl')

    # --- Remove symbols that TTS reads as English words ---
    text = re.sub(r'[#$%^*_+=~`|\\]', '', text)

    # --- Normalize repeated punctuation ---
    text = re.sub(r'\.{4,}', '...', text)     # 4+ dots → ellipsis
    text = re.sub(r'!{3,}', '!!', text)
    text = re.sub(r'\?{3,}', '??', text)
    text = re.sub(r'[,]{2,}', ',', text)
    text = re.sub(r';{2,}', ';', text)
    text = re.sub(r':{2,}', ':', text)

    # --- Normalize excessive newlines ---
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()

def escape_ssml(text):
    """Escape SSML special characters, but preserve existing SSML tags like <break>."""
    # First, temporarily protect known SSML tags
    breaks = []
    def _save_break(m):
        breaks.append(m.group(0))
        return f"\x00BREAK{len(breaks)-1}\x00"
    text = re.sub(r'<break\s+[^>]+/?>', _save_break, text)

    # Escape remaining < > &
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    # Restore protected SSML tags
    for i, b in enumerate(breaks):
        text = text.replace(f"\x00BREAK{i}\x00", b)
    return text

def build_ssml(text, profile=None):
    """Wrap pre-cleaned text in SSML tags.
    Text should already be escaped and normalized before calling this.
    """
    profile = profile or VOICE_PRESETS[VOICE_PROFILE]
    return f"""
<speak xmlns="http://www.w3.org/2001/10/synthesis"
       xml:lang="en-US">

    <voice name="{profile["voice"]}">
        <prosody rate="{profile["rate"]}" pitch="{profile["pitch"]}">
            {text}
        </prosody>
    </voice>

</speak>
"""

async def build_timeline(valid_files):
    async def probe(path):
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffprobe","-v","error",
                "-show_entries","format=duration",
                "-of","default=noprint_wrappers=1:nokey=1",
                path
            ],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        try:
            return float(result.stdout.strip() or 0)
        except:
            return 0.0

    durations = await asyncio.gather(*[
        probe(path) for _, path in valid_files
    ])

    cum = [0]
    for d in durations:
        cum.append(cum[-1] + d)

    return durations, cum

def safe_filename(name):
    name = re.sub(r'[<>:"/\\\\|?*]', '', name)
    name = re.sub(r'\s+', '_', name)
    return name[:120]
    
def checkpoint_path(name):
    return f"checkpoint_{name}.json"
    
def load_checkpoint(path):
    if not os.path.exists(path):
        return {"done": {}}

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "done" not in data or not isinstance(data["done"], dict):
        data["done"] = {}

    return data

def save_checkpoint(path, data):
    tmp = path + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    os.replace(tmp, path)    

async def synthesize(text, path, edge_api_semaphore, profile=None):

    profile = profile or VOICE_PRESETS[VOICE_PROFILE]

    plain = re.sub(r'<[^>]+>', '', text)

    if not plain or len(plain.strip()) < 20:
        return False

    for attempt in range(RETRIES):

        try:
            async with edge_api_semaphore:

                await asyncio.sleep(random.uniform(0.3, EDGE_COOLDOWN))

                ssml = build_ssml(text, profile)

                communicate = edge_tts.Communicate(
                    ssml,
                    voice=profile["voice"]
                )

                await communicate.save(path)

            if not os.path.exists(path):
                raise RuntimeError("No file generated")

            if os.path.getsize(path) < CORRUPT_THRESHOLD:
                raise RuntimeError("Empty audio")

            return True

        except Exception as e:
            backoff = min(2 ** attempt, 5) + random.random() * 0.5
            print(f"Synthesis failed ({attempt+1}): {e}")
            await asyncio.sleep(backoff)

    return False
    
# =========================
# TEXT CLEANING
# =========================
def clean_text(text):

    text = re.sub(r'http\S+', '', text)

    # remove standalone page numbers
    text = re.sub(r'(?m)^\s*\d+\s*$', ' ', text)

    # remove "Page 42"
    text = re.sub(r'(?i)(?:page\s*)?\b\d+\b(?=\s*$)', '', text)

    # remove control chars except newline
    text = re.sub(r'[\x00-\x09\x0B-\x1F\x7F]', ' ', text)

    # normalize spaces only
    text = re.sub(r'[ \t]+', ' ', text)

    # normalize excessive newlines
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()
    
# =========================
# CHAPTER DETECTION
# =========================
def extract_bookmarks(pdf_path):
    doc = fitz.open(pdf_path)
    toc = doc.get_toc()
    doc.close()

    chapters = []

    for level, title, page in toc:
        if level > 2:
            continue

        # Convert to zero-based index
        page -= 1

        # Ignore invalid bookmark pages
        if page < 0:
            continue

        chapters.append({
            "title": title.strip(),
            "page": page
        })

    return chapters
    
def detect_chapters(pages):
    chapters = []

    for p in pages:
        text = p["text"]

        # RULE: ALL CAPS / short lines
        if len(text) < 80 and text.isupper():
            chapters.append({"title": text, "page": p["page"]})
            continue

        # RULE: numbered chapters
        if re.match(r"^(chapter|part|section)\s+\d+", text, re.I):
            chapters.append({"title": text, "page": p["page"]})

    # fallback
    if not chapters:
        chapters = [{"title": "Start", "page": 0}]

    return chapters
    
def detect_chapters_by_font(pdf_path):
    doc = fitz.open(pdf_path)
    chapters = []
    for i, page in enumerate(doc):
        data = page.get_text("dict")
        for b in data["blocks"]:
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    t = span["text"].strip()
                    if len(t) < 5:
                        continue
                    if (
                        span["size"] >= 14
                        and len(t.split()) < 12
                        and re.search(r'(chapter|part|section|\d+)', t, re.I)
                    ):
                        chapters.append({"title": t, "page": i})
    doc.close()
    seen, clean = set(), []
    for ch in chapters:
        key = ch["title"].lower()
        if key not in seen:
            seen.add(key)
            clean.append(ch)
    return clean

# =========================
# PAGE & CHUNK POSITIONS
# =========================
def extract_pages(pdf_path):
    doc = fitz.open(pdf_path)
    pages = []

    for i, page in enumerate(doc):
        text = page.get_text("text", sort=True)

        pages.append({
            "page": i,
            "text": clean_text(text)
        })

    doc.close()
    return pages
    
# =========================
# SMART CHUNKING
def split_text(text, size):

    sentences = re.split(
        r'(?<=[.!?])\s+',
        text
    )

    chunks = []
    current = ""
    carryover = ""

    for s in sentences:

        # giant sentence protection
        if len(s) > size * 2:

            parts = [
                s[i:i + size]
                for i in range(0, len(s), size)
            ]

            for part in parts:

                if current:
                    chunks.append(current.strip())
                    current = ""

                chunks.append(part.strip())

            continue

        s = carryover + s
        carryover = ""

        adaptive_limit = size
        
        def safe_join(a, b):
            if not a:
                return b or ""
            if not b:
                return a

            a = a.rstrip()
            b = b.lstrip()

            if a[-1].isalnum() and b[0].isalnum():
                return a + " " + b

            return a + b
        if len(current) + len(s) <= adaptive_limit:
            current = safe_join(current, s)

        else:
            if current:
                chunks.append(current.strip())

            current = s

        # bracket balancing
        opens = (
            current.count("(")
            + current.count("[")
            + current.count("{")
        )
        if not current:
            current = ""
        closes = (
            current.count(")")
            + current.count("]")
            + current.count("}")
        )

        if opens > closes:

            bracket_pos = max(
                current.rfind("("),
                current.rfind("["),
                current.rfind("{")
            )

            if bracket_pos != -1:
                carryover = current[bracket_pos:]
                current = current[:bracket_pos]

    if current:
        chunks.append(current.strip())

    if carryover:
        chunks.append(carryover.strip())

    return chunks
    
# =========================
# UTILS
# =========================
def is_corrupt(path):
    if not os.path.exists(path):
        return True
    try:
        return os.path.getsize(path) < CORRUPT_THRESHOLD
    except:
        return True
        
async def run_cmd(cmd):
    result = await asyncio.to_thread(
        subprocess.run,
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    if result.returncode != 0:
        print(result.stderr)
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd
        )

    return result
    
async def trim_mp3_safe(path, start=0, end=0):

    tmp = path + ".tmp.mp3"

    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    try:
        total = float(result.stdout.strip())
    except:
        return

    # protect tiny chunks
    if total <= (start + end + 1):
        return

    abs_start = max(0, start)
    abs_end = total - end if end > 0 else total

    if abs_end <= abs_start:
        return

    cmd = [
        "ffmpeg",
        "-y",
        "-i", path,
        "-ss", str(abs_start),
        "-t", str(abs_end - abs_start),
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        tmp
    ]

    try:
        await run_cmd(cmd)
        shutil.move(tmp, path)

    except:
        if os.path.exists(tmp):
            os.remove(tmp)
            
def build_chapter_texts(chapter_ranges, pages):
    chapter_texts = []

    for ch in chapter_ranges:
        text = "\n".join(
            pages[i]["text"]
            for i in range(ch["start_page"], ch["end_page"])
        )

        # Normalize full chapter text once upfront
        text = normalize_english_text(text)

        chapter_texts.append({
            "title": ch["title"],
            "text": text
        })

    return chapter_texts

def build_chapter_markers(chapter_map, cum):
    markers = []

    for i, ch in enumerate(chapter_map):
        start_chunk = ch["start_chunk"]

        start_time = cum[start_chunk]

        if i + 1 < len(chapter_map):
            end_time = cum[chapter_map[i + 1]["start_chunk"]]
        else:
            end_time = cum[-1]

        markers.append({
            "title": ch["title"],
            "start": start_time,
            "end": end_time,
            "chunk": start_chunk
        })

    return markers
    
def build_chunks(ranges, pages):
    chapter_texts = build_chapter_texts(ranges, pages)
    chunks, chapter_map = chunk_chapters(chapter_texts, DEFAULT_CHUNK_SIZE)
    return chunks, chapter_map
    
def build_chapter_ranges(chapters, pages):
    chapters = [
        ch for ch in chapters
        if 0 <= ch["page"] < len(pages)
    ]

    chapters.sort(key=lambda x: x["page"])

    ranges = []
    for i, ch in enumerate(chapters):
        start = ch["page"]
        end = chapters[i + 1]["page"] if i + 1 < len(chapters) else len(pages)

        ranges.append({
            "title": ch["title"],
            "start_page": start,
            "end_page": end
        })

    return ranges
    
def chunk_chapters(chapter_texts, size):

    all_chunks = []
    chapter_map = []
    global_index = 0

    for ch in chapter_texts:

        chunks = split_text(ch["text"], size)
        start_idx = global_index

        for i, c in enumerate(chunks):

            safe = c
            # Escape SSML special chars BEFORE inserting break tags
            safe = escape_ssml(safe)
            safe = re.sub(r'\n{2,}', '<break time="500ms"/>', safe)
            safe = re.sub(r'\n', ' ', safe).strip()

            all_chunks.append({
                "text": safe,
                "is_chapter_start": i == 0,
                "chapter_title": ch["title"],
                "chapter_index": len(chapter_map)
            })

            global_index += 1

        # ✅ ONLY ONCE PER CHAPTER
        chapter_map.append({
            "title": ch["title"],
            "start_chunk": start_idx,
            "end_chunk": global_index - 1
        })

    return all_chunks, chapter_map
    
# =========================
# VALIDATE MP3(Chunks) → OUTPUT
# =========================
async def validate_mp3(path):
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "ffprobe","-v","error",
            "-show_entries","format=duration",
            "-of","default=noprint_wrappers=1:nokey=1",
            path
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    if result.returncode != 0:
        return False

    try:
        return float(result.stdout.strip()) > 0.25
    except:
        return False
        
# =========================
# MERGE MP3 → OUTPUT
# =========================
async def merge_mp3(folder, output):

    files = sorted(
        f for f in os.listdir(folder)
        if re.fullmatch(r"\d{6}\.mp3", f)
    )

    if not files:
        raise FileNotFoundError(f"No MP3 chunks found in {folder}")

    valid = []

    for f in files:
        full = str(pathlib.Path(folder) / f)

        try:
            if os.path.getsize(full) < 1500:
                continue

            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    full
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace"
            )

            if result.returncode != 0:
                continue

            duration = float(result.stdout.strip() or 0)

            if duration < 0.35:
                continue

            valid.append((int(f[:6]), full))

        except:
            continue

    if not valid:
        raise RuntimeError("No valid MP3 chunks to merge")

    # 🔥 CRITICAL FIX: restore strict ordering
    valid.sort(key=lambda x: x[0])

    list_file = os.path.join(folder, "concat_list.txt")

    with open(list_file, "w", encoding="utf-8", newline="\n") as lf:
        for _, path in valid:
            file_path = pathlib.Path(path).resolve().as_posix()
            lf.write(f"file '{file_path}'\n")

    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,

        # IMPORTANT: normalize timing drift
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",

        # re-encode ALWAYS (prevents drift accumulation)
        "-c:a", "libmp3lame",
        "-b:a", "128k",
        "-ar", "24000",
        "-ac", "1",

        output
    ]

    result = await asyncio.to_thread(
        subprocess.run,
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    os.remove(list_file)

    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError("FFmpeg concat failed")
    return valid
# =========================
# DURATION / CHAPTER TEXT
# =========================
async def get_durations(folder):
    files = sorted(
    f for f in os.listdir(folder)
    if re.fullmatch(r"\d{6}\.mp3", f)
    )
    async def probe(f):
        path = os.path.join(folder,f)
        result = await asyncio.to_thread(subprocess.run,[
            "ffprobe","-v","error","-show_entries","format=duration",
            "-of","default=noprint_wrappers=1:nokey=1", path
        ], stdout=subprocess.PIPE, text=True,encoding="utf-8", errors="replace")
        out = result.stdout.strip()
        return float(out) if out else 0
    return files, await asyncio.gather(*[probe(f) for f in files])

def seconds_to_hms(s):
    h = int(s//3600)
    m = int((s%3600)//60)
    sec = int(s%60)
    return f"{h:02d}:{m:02d}:{sec:02d}"
        
# =========================
# FFmetadata → M4B
# =========================
def generate_ffmetadata_for_m4b(chapters, cum, out_meta):
    lines = [";FFMETADATA1"]

    for ch in chapters:
        start = int(ch["start"] * 1000)
        end = int(ch["end"] * 1000)

        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start}",
            f"END={end}",
            f"title={ch['title']}"
        ]

    with open(out_meta, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        
async def mp3_to_m4b(mp3,m4b,meta):
    await run_cmd(["ffmpeg","-y","-i",mp3,"-i",meta,"-map_metadata","1","-map","0:a","-c:a","aac","-b:a","128k",m4b])

async def worker(
    queue,
    checkpoint,
    checkpoint_file,
    progress,
    edge_api_semaphore
):
    try:
        while True:
            item = await queue.get()

            if item is None:
                queue.task_done()
                break

            idx, text, path, is_chapter_start, chapter_title, chapter_index = item
            idx_str = str(idx)

            try:
                # 🔥 HARD SKIP (already done OR file exists)
                if (
                    checkpoint["done"].get(idx_str)
                    and not is_corrupt(path)
                ):
                    continue

                if len(text.strip()) < 5:
                    print(f"[SKIP EMPTY CHUNK] {idx}")
                    continue

                ok = await synthesize(
                    text,
                    path,
                    edge_api_semaphore
                )

                if ok:

                    if idx == 0:

                        trim_start = FIRST_CHUNK_TRIM_START

                    elif is_chapter_start:

                        trim_start = CHAPTER_TRIM_START

                    else:

                        trim_start = NORMAL_TRIM_START

                    await trim_mp3_safe(
                        path,
                        trim_start,
                        TRIM_END
                    )

                    if not await validate_mp3(path):
                        raise RuntimeError("Trimmed MP3 invalid")

                    checkpoint["done"][idx_str] = True

                    save_checkpoint(
                        checkpoint_file,
                        checkpoint
                    )

                    progress.update(1)
                else:
                    print(f"[FAIL] permanent chunk: {idx}")

            except Exception as e:
                print(f"[ERROR] chunk {idx} -> {e}")

            finally:
                queue.task_done()

    except asyncio.CancelledError:
        pass
        



# =========================
# MAIN
# =========================
async def main():
    def out_path(folder, filename):
        return os.path.join(folder, filename)
        
    def ensure_dir(path):
        os.makedirs(path, exist_ok=True)
        return path
    edge_api_semaphore = asyncio.Semaphore(
        EDGE_CONCURRENCY
    )

    pdf_dir = "pdf"

    pdfs = [
        f for f in os.listdir(pdf_dir)
        if f.lower().endswith(".pdf")
    ]

    if not pdfs:
        print("No PDFs found.")
        return

    # =========================
    # PDF SELECTOR
    # =========================
    print("\nSelect PDF file (0 = process all):")

    for i, f in enumerate(pdfs, 1):
        print(f"{i}: {f}")

    print("0: process all")

    while True:

        choice = input("Choice: ").strip()

        if not choice.isdigit():
            print("Enter a number.")
            continue

        choice = int(choice)

        if 0 <= choice <= len(pdfs):
            break

        print("Invalid choice.")

    selected = (
        pdfs
        if choice == 0
        else [pdfs[choice - 1]]
    )

    # =========================
    # PROCESS PDFs
    # =========================
    for pdf in selected:

        pdf_path = os.path.join(
            pdf_dir,
            pdf
        )

        name = os.path.splitext(pdf)[0]

        global VOICE_PROFILE

        VOICE_PROFILE = "documentary"

        print("\n=========================")
        print("Processing:", pdf)
        print("Voice:", VOICE_PROFILE)
        print("=========================")

        # =========================
        # LOAD PAGES
        # =========================
        pages = extract_pages(pdf_path)
        print("\n========== FIRST 5 PAGES ==========")
        for i in range(min(5, len(pages))):
            print(f"\n----- PAGE {i} -----")
            print(pages[i]["text"][:500])
        print("===================================\n")
        
        # =========================
        # DETECT CHAPTERS
        # =========================
        chapters = extract_bookmarks(pdf_path)
        print("\n========== BOOKMARKS ==========")
        print(chapters[:10])

        if not chapters:
            chapters = detect_chapters_by_font(pdf_path)

        if not chapters:
            chapters = detect_chapters(pages)

        # ALWAYS guarantee page 0 exists
        chapters = sorted(chapters, key=lambda x: x["page"])
        # Remove invalid bookmarks
        chapters = [
            ch for ch in chapters
            if 0 <= ch["page"] < len(pages)
        ]

        chapters.sort(key=lambda x: x["page"])
        if not chapters or chapters[0]["page"] != 0:
            chapters.insert(0, {"title": "Start", "page": 0})

        # optional safety: ensure last boundary
        if chapters[-1]["page"] != len(pages):
            chapters.append({"title": "End", "page": len(pages)})

        print("\n========== FINAL CHAPTERS ==========")
        for ch in chapters[:10]:
            print(ch)

        print(f"\nDetected chapters: {len(chapters)}")

        # =========================
        # BUILD CHUNKS
        # =========================
        ranges = build_chapter_ranges(chapters, pages)

        print("\n========== CHAPTER RANGES ==========")
        for r in ranges[:10]:
            print(r)

        chunks, chapter_map = build_chunks(ranges, pages)

        print(f"\nChunks generated: {len(chunks)}")

        print("\n========== FIRST 10 CHUNKS ==========")
        for i in range(min(10, len(chunks))):
            print(f"\nChunk {i}")
            print("Chapter:", chunks[i]["chapter_title"])
            print(chunks[i]["text"][:300])

        # =========================
        # OUTPUT FOLDER
        # =========================
        chunk_dir = ensure_dir(os.path.join("chunks", name))
        output_dir = ensure_dir("mp3")

        # =========================
        # CHECKPOINT
        # =========================
        checkpoint_file = checkpoint_path(name)

        checkpoint = load_checkpoint(
            checkpoint_file
        )
        # =========================
        # BUILD QUEUE
        # =========================
        queue = asyncio.Queue()

        for i, chunk in enumerate(chunks):

            t = chunk["text"]

            path = os.path.join(
                chunk_dir,
                f"{i:06d}.mp3"
            )

            if (
                str(i) not in checkpoint["done"]
                or is_corrupt(path)
            ):
                queue.put_nowait((
                    i,
                    t,
                    path,
                    chunk["is_chapter_start"],
                    chunk.get("chapter_title", ""),
                    chunk.get("chapter_index", -1)
                ))
        pending = queue.qsize()
        if pending == 0:
            print("Nothing to process")
            continue

        progress = tqdm(
            total=pending,
            desc=f"{name}"
        )

        # =========================
        # START WORKERS
        # =========================
        workers = [

            asyncio.create_task(

                worker(
                    queue,
                    checkpoint,
                    checkpoint_file,
                    progress,
                    edge_api_semaphore
                )

            )

            for _ in range(MAX_WORKERS)
        ]

        await queue.join()

        for _ in range(MAX_WORKERS):
            queue.put_nowait(None)

        await asyncio.gather(*workers, return_exceptions=True)

        progress.close()

        # =========================
        # MERGE
        # =========================
        output_mp3 = os.path.join(
            output_dir,
            safe_filename(f"{name}.mp3")
        )

        print("Merging MP3...")
        valid_files = await merge_mp3(chunk_dir, output_mp3)

        durations, cum = await build_timeline(valid_files)
        chapter_markers = build_chapter_markers(chapter_map, cum)

        meta_file = os.path.join(
            output_dir,
            safe_filename(f"{name}_chapters.ffmeta")
        )

        generate_ffmetadata_for_m4b(chapter_markers, cum, meta_file)

        # =========================
        # TELEGRAM MARKDOWN
        # =========================
        telegram_path = os.path.join(output_dir, safe_filename(f"{name}_telegram.md"))
        with open(telegram_path, "w", encoding="utf-8") as f:
            for ch in chapter_markers:
                ts = seconds_to_hms(ch["start"])
                line = f"{ts} {ch['title']}\n"
                f.write(line)

        print("Saved:", telegram_path)

        # =========================
        # FFMETADATA + M4B
        # =========================
        meta_file = os.path.join(
            output_dir,
            safe_filename(f"{name}_chapters.ffmeta")
        )
        generate_ffmetadata_for_m4b(chapter_markers, cum, meta_file)
        print("Saved:", meta_file)
        output_m4b = os.path.join(
            output_dir,
            safe_filename(f"{name}.m4b")
        )
        print("Creating M4B...")

        await mp3_to_m4b(
            output_mp3,
            output_m4b,
            meta_file
        )

        print("Saved:", output_m4b)
        save_checkpoint(
            checkpoint_file,
            checkpoint
        )

        print("Finished:", pdf)

if __name__=="__main__":
    asyncio.run(main())
