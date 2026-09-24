import asyncio
import edge_tts
import fitz
import os
import subprocess
import re
import time
from googletrans import Translator
from random import randint

# =========================
# CONFIG
# =========================
VOICE = "ar-AE-HamdanNeural"
TRIM_START = 25.35
TRIM_END = 5.5
MAX_WORKERS = 6
RETRIES = 10
EDGE_COOLDOWN = 0.5
DEFAULT_CHUNK_SIZE = 2500
CORRUPT_THRESHOLD = 1500

# =========================
# CLEANING + META LEAK REMOVAL
# =========================
def clean_text(text):
    # Remove short lines at the end (meta leaks, references)
    lines = text.split('\n')
    while lines and (len(lines[-1].strip()) < 5 or re.match(r'^\s*(page|p|www|http|note|ref)', lines[-1], re.I)):
        lines.pop()
    text = '\n'.join(lines)
    text = re.sub(r'\\+', '', text)
    text = re.sub(r'\[[0-9]+\]', '', text)
    text = re.sub(r'[<>]', '', text)
    return text.strip()

def normalize_arabic_text(text):
    """Comprehensive Arabic text normalization for clean TTS pronunciation.
    Handles: English text, special symbols, diacritics, tatweel,
    punctuation issues, numbers, and other PDF extraction artifacts.
    """
    # --- Phase 0: Strip bullet points and list markers FIRST ---
    # Must run before English text removal (lettered lists need the letter)
    # and before line joining (markers must still be at line starts)
    # Unicode bullets
    text = re.sub(r'(?m)^\s*[\u2022\u2023\u2043\u25AA\u25AB\u25A0\u25A1\u25CF\u25CB\u25C6\u25C7\u2605\u2606\u27A4\u25B6\u25B8\u25BE\u25BF\u279C]\s*', '', text)
    # Dash/asterisk bullets at line start
    text = re.sub(r'(?m)^\s*[-*]\s+', '', text)
    # Numbered lists: "1. ", "2) ", "(3) "
    text = re.sub(r'(?m)^\s*\(?\d+\)?[.)]\s+', '', text)
    # Lettered lists: "a. ", "b) ", "(c) " (before English removal!)
    text = re.sub(r'(?m)^\s*\(?[a-zA-Z]\)?[.)]\s+', '', text)
    # Arabic-Indic numbered lists: "١. ", "٢) " etc.
    text = re.sub(r'(?m)^\s*[\u0660-\u0669]+[.)]\s+', '', text)
    # Arabic star bullet: ٭
    text = re.sub(r'(?m)^\s*٭\s*', '', text)

    # --- Phase 1: Remove non-Arabic content ---
    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)

    # Remove email addresses
    text = re.sub(r'\S+@\S+\.\S+', '', text)

    # Remove HTML/XML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Remove all English/Latin characters (primary noise source in Arabic TTS)
    text = re.sub(r'[a-zA-Z]+', '', text)

    # --- Phase 2: Clean Arabic-specific artifacts ---
    # Remove tatweel/kashida (elongation character U+0640) — causes stretched pronunciation
    text = re.sub(r'\u0640', '', text)

    # Remove Arabic diacritics/tashkeel — TTS handles pronunciation on its own;
    # diacritics can cause double-reading of short vowels
    text = re.sub(
        r'[\u0617-\u061A\u064B-\u0652\u0656-\u065F\u0670'
        r'\u06D6-\u06DC\u06DF-\u06E4\u06E7\u06E8\u06EA-\u06ED]',
        '', text
    )

    # --- Phase 3: Normalize punctuation ---
    # En-dash / em-dash → space (dash pronunciation is awkward in Arabic)
    text = re.sub(r'[\u2013\u2014]', ' ', text)

    # Normalize various quote styles to Arabic equivalents
    text = re.sub(r'[“”«»„‟‹›]', '“', text)
    text = re.sub(r"[‘’‚‛′″]", '‘', text)

    # Normalize repeated punctuation (both English and Arabic forms)
    text = re.sub(r'\.{3,}', '…', text)    # 3+ dots → ellipsis
    text = re.sub(r'؟{2,}', '؟', text)
    text = re.sub(r'!{2,}', '!', text)
    text = re.sub(r'،{2,}', '،', text)
    text = re.sub(r',{2,}', '،', text)      # English commas → single Arabic comma
    text = re.sub(r';{2,}', ';', text)      # repeated semicolons
    text = re.sub(r':{2,}', ':', text)

    # Remove HTML entities (e.g. &amp; &lt; &gt; &nbsp; etc.)
    text = re.sub(r'&\w+;', '', text)
    text = re.sub(r'&#\d+;', '', text)

    # Remove symbols that TTS reads as English words or causes artifacts
    text = re.sub(r'[#&%$@*+=~`^|\\<>]', '', text)

    # --- Phase 4: Handle brackets and parentheses ---
    # Remove square bracket content (almost always footnotes/references)
    text = re.sub(r'\[([^\]]*)\]', '', text)

    # Remove parentheses — orphaned brackets cause awkward TTS pauses;
    # keep the content inside since it may be meaningful Arabic text
    text = text.replace('(', ' ').replace(')', ' ')
    text = text.replace('{', ' ').replace('}', ' ')

    # --- Phase 5: Number handling ---
    # Convert Western digits (0-9) to Arabic-Indic digits (٠-٩)
    # so the TTS engine reads them with Arabic pronunciation
    digit_map = str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩')
    text = text.translate(digit_map)

    # --- Phase 6: Punctuation spacing (Arabic convention) ---
    # No space before punctuation
    text = re.sub(r'\s+،', '،', text)
    text = re.sub(r'\s+؟', '؟', text)
    text = re.sub(r'\s+!', '!', text)
    text = re.sub(r'\s+:', ':', text)
    text = re.sub(r'\s+;', ';', text)
    text = re.sub(r'\s+\.', '.', text)
    text = re.sub(r'\s+…', '…', text)

    # Ensure space after punctuation if missing
    text = re.sub(r'،(?!\s|\n|$)', '، ', text)
    text = re.sub(r'؟(?!\s|\n|$)', '؟ ', text)
    text = re.sub(r'!(?!\s|\n|$)', '! ', text)
    text = re.sub(r'\.(?!\s|\n|$|\d)', '. ', text)
    text = re.sub(r';(?!\s|\n|$)', '; ', text)
    text = re.sub(r':(?!\s|\n|$)', ': ', text)
    text = re.sub(r'…(?!\s|\n|$)', '… ', text)

    # --- Phase 7: Final cleanup ---
    # Collapse multiple spaces / strip
    text = re.sub(r'\s+', ' ', text).strip()

    # Remove leading/trailing punctuation fragments left after cleaning
    text = re.sub(r'^[\s،.؟!:;,…]+', '', text)
    text = re.sub(r'[\s،.؟!:;,…]+$', '', text)

    return text

# =========================
# SSML BUILDER
# =========================
def build_ssml(text):
    text = text.replace(" .", ".").replace(" ،", "،")
    return f"""<speak xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="ar-AE">
  <voice name="{VOICE}">
    <prosody rate="0%" pitch="0%" volume="0%">
      {text}
    </prosody>
  </voice>
</speak>"""

# =========================
# SMART CHUNKING
# =========================
def split_text(text, size):
    sentences = re.split(r'(?<=[.!؟]) +', text)
    chunks, current = [], ""
    carryover = ""
    for s in sentences:
        s = carryover + s
        carryover = ""
        adaptive_limit = size if len(s) <= size else size * 2
        if len(current) + len(s) <= adaptive_limit:
            current += " " + s
        else:
            if current: chunks.append(current.strip())
            current = s
        # bracket balancing
        opens = current.count('(') + current.count('[') + current.count('{')
        closes = current.count(')') + current.count(']') + current.count('}')
        if opens > closes:
            bracket_pos = max(current.rfind('('), current.rfind('['), current.rfind('{'))
            carryover = current[bracket_pos:]
            current = current[:bracket_pos]
    if current: chunks.append(current.strip())
    if carryover: chunks.append(carryover.strip())
    return chunks

# =========================
# AUDIO UTILS
# =========================
_duration_cache = {}

def get_duration(path):
    if path in _duration_cache: return _duration_cache[path]
    try:
        res = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True)
        duration = float(res.stdout.strip() or 0)
    except:
        duration = 0.0
    _duration_cache[path] = duration
    return duration

def trim_audio(path):
    tmp = path + ".tmp.mp3"
    orig = get_duration(path)
    keep = max(0.1, orig - TRIM_START - TRIM_END)
    if keep > 0.1:
        try:
            subprocess.run([
                "ffmpeg","-y","-ss", str(TRIM_START), "-i", path,
                "-t", str(keep), "-map_metadata","-1","-vn",
                "-af","silenceremove=start_periods=1:start_duration=0.2:start_threshold=-45dB,areverse,"
                     "silenceremove=start_periods=1:start_duration=0.2:start_threshold=-45dB,areverse",
                "-c:a","libmp3lame","-b:a","192k", tmp
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if os.path.exists(tmp):
                os.replace(tmp, path)
                _duration_cache.pop(path, None)
        except Exception as e:
            print(f"Trim failed: {e}")
            if os.path.exists(tmp): os.remove(tmp)

def is_corrupt(path):
    if not os.path.exists(path): return True
    if os.path.getsize(path) < CORRUPT_THRESHOLD: return True
    dur = get_duration(path)
    return dur < 0.3

# =========================
# GENERATE AUDIO CHUNK
# =========================
async def generate_chunk(idx, text, chunk_dir):
    filename = f"chunk_{idx:06d}.mp3"
    path = os.path.join(chunk_dir, filename)
    tmp = path + ".part"
    if os.path.exists(path) and not is_corrupt(path): return True
    ssml = build_ssml(text)
    for attempt in range(RETRIES):
        try:
            communicate = edge_tts.Communicate(text=ssml, voice=VOICE)
            audio = bytearray()
            async for msg in communicate.stream():
                if msg["type"]=="audio": audio.extend(msg["data"])
            if len(audio) < 500: raise RuntimeError("empty audio")
            with open(tmp,"wb") as f: f.write(audio)
            os.replace(tmp,path)
            trim_audio(path)
            if is_corrupt(path): raise RuntimeError("invalid")
            await asyncio.sleep(EDGE_COOLDOWN)
            return True
        except Exception as e:
            print(f"\nChunk {idx} attempt {attempt+1} failed: {e}")
            if os.path.exists(tmp): os.remove(tmp)
            await asyncio.sleep(min(10, 2 ** attempt + randint(0,1000)/1000))
    return False

# =========================
# GENERATE ALL CHUNKS
# =========================
async def generate_all(chunks, chunk_dir):
    os.makedirs(chunk_dir, exist_ok=True)
    queue = asyncio.Queue()
    for idx, text in enumerate(chunks): await queue.put((idx,text))
    results = {"done":0,"failed":[]}
    start_time = time.time()

    async def worker():
        while True:
            try:
                idx,text = await asyncio.wait_for(queue.get(),timeout=5)
            except asyncio.TimeoutError:
                return
            try:
                ok = await generate_chunk(idx,text,chunk_dir)
                if ok: results["done"] += 1
                else: results["failed"].append(idx)
            finally: queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(MAX_WORKERS)]
    while results["done"] + len(results["failed"]) < len(chunks):
        await asyncio.sleep(0.5)
        done = results["done"]
        failed = len(results["failed"])
        total = len(chunks)
        elapsed = int(time.time()-start_time)
        percent = int((done+failed)/total*100)
        bar_len = 30
        filled_len = int(bar_len*(done+failed)/total)
        bar = "█"*filled_len + "-"*(bar_len-filled_len)
        remaining = total - (done+failed)
        avg_chunk = elapsed / max(done+failed,1)
        eta_sec = int(avg_chunk*remaining)
        eta_min, eta_sec = divmod(eta_sec,60)
        print(f"\r[{bar}] {done+failed}/{total} chunks ({percent}%) "
              f"Elapsed {elapsed}s ETA {eta_min}m{eta_sec:02d}s Failed: {failed}", end="")
    await queue.join()
    for w in workers: w.cancel()
    print()
    return results["failed"]

# =========================
# VERIFY / REPAIR
# =========================
def verify_chunks(chunks, chunk_dir):
    return [idx for idx in range(len(chunks)) if is_corrupt(os.path.join(chunk_dir,f"chunk_{idx:06d}.mp3"))]

async def repair_chunks(chunks, chunk_dir, failed):
    if not failed: return
    total = len(failed)
    print(f"\nRepairing {total} chunks...")
    start = time.time()
    for i, idx in enumerate(failed,1):
        await generate_chunk(idx,chunks[idx],chunk_dir)
        elapsed = int(time.time() - start)
        print(f"\rRepaired {i}/{total} chunks. Elapsed {elapsed}s", end="")
    print()

# =========================
# MERGE / M4B
# =========================
def merge_chunks(chunk_dir, output):
    files = sorted(f for f in os.listdir(chunk_dir) if f.startswith("chunk_") and f.endswith(".mp3"))
    list_file = os.path.join(chunk_dir, "list.txt")
    with open(list_file,"w",encoding="utf8") as f:
        for name in files:
            full_path = os.path.abspath(os.path.join(chunk_dir,name)).replace("'", r"'\''")
            f.write(f"file '{full_path}'\n")
    subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",list_file,"-c","copy",os.path.abspath(output)], check=True)
    os.remove(list_file)

def convert_to_m4b(mp3):
    m4b = mp3.replace(".mp3",".m4b")
    subprocess.run(["ffmpeg","-y","-i",mp3,"-c:a","aac","-b:a","64k",m4b], check=True)

# =========================
# Classical TRANSLATION
# =========================
def translate_text(text, translator, chunk_size=2500):
    chunks = [
        text[i:i+chunk_size].strip()
        for i in range(0, len(text), chunk_size)
        if text[i:i+chunk_size].strip()
    ]

    translated_chunks = []

    for i, chunk in enumerate(chunks, 1):
        for attempt in range(5):
            try:
                result = translator.translate(chunk, dest='ar').text

                if not result.strip() or result == chunk:
                    raise Exception("Translation failed or unchanged")

                translated_chunks.append(result)
                #print(f"Translated chunk {i}/{len(chunks)}")
                time.sleep(1.2)
                break

            except Exception as e:
                print(f"Chunk {i} failed (attempt {attempt+1}): {e}")
                time.sleep(2 + attempt)

                if attempt == 4:
                    print("⚠️ Falling back to original text")
                    translated_chunks.append(chunk)

    return " ".join(translated_chunks)
    
# =========================
# MAIN PIPELINE
# =========================
async def main():
    pdf_folder = "pdf"
    chunk_root = "chunks"
    output_root = "mp3"
    os.makedirs(chunk_root, exist_ok=True)
    os.makedirs(output_root, exist_ok=True)

    pdfs = [f for f in os.listdir(pdf_folder) if f.lower().endswith(".pdf")]
    if not pdfs:
        print("No PDFs found in 'pdf' folder.")
        return

    print("\nSelect PDF file (0 = process all files):")
    for i,f in enumerate(pdfs,1): print(f"{i}: {f}")
    print("0: process all")
    while True:
        user = input("Choice: ").strip()
        if user.lower() in ["exit","quit"]: return
        if user.isdigit():
            choice = int(user)
            if 0<=choice<=len(pdfs): break
        print("Invalid choice")
    selected = pdfs if choice==0 else [pdfs[choice-1]]
    translator = Translator()

    for pdf_name in selected:
        print("\nProcessing:", pdf_name)
        pdf_path = os.path.join(pdf_folder,pdf_name)
        base = os.path.splitext(pdf_name)[0]
        chunk_dir = os.path.join(chunk_root, base)
        output_file = os.path.join(output_root, base+".mp3")

        print("Reading PDF...")
        doc = fitz.open(pdf_path)
        all_text = []
        for page in doc:
            blocks = page.get_text("blocks")
            blocks.sort(key=lambda b:(b[1],b[0]))
            for b in blocks:
                t = b[4].strip()
                if t: all_text.append(t)
        doc.close()
        text = clean_text("\n".join(all_text))
        print("RAW length:", len("\n".join(all_text)))
        print("CLEAN length:", len(text))

        # Detect + translate if needed
        try: detected = translator.detect(text[:5000]).lang
        except: detected = 'en'

        if detected != 'ar':
            print(f"Detected language: {detected}, translating to Arabic...")
            text_ar = translate_text(text, translator)
        else:
            print("Detected language is Arabic, skipping translation.")
            text_ar = text

        # Normalize Arabic text for clean TTS pronunciation
        text_ar = normalize_arabic_text(text_ar)
        print("NORMALIZED length:", len(text_ar))

        # Split & generate audio
        chunks = split_text(text_ar, DEFAULT_CHUNK_SIZE)
        print("Number of chunks:", len(chunks))
        if input("Proceed? (y/n): ").lower() != "y": continue

        failed = await generate_all(chunks, chunk_dir)
        await repair_chunks(chunks, chunk_dir, failed)
        while True:
            missing = verify_chunks(chunks, chunk_dir)
            if not missing: break
            await repair_chunks(chunks, chunk_dir, missing)

        print("\nMerging audiobook...")
        merge_chunks(chunk_dir, output_file)
        print("Creating M4B...")
        convert_to_m4b(output_file)
        print("Finished:", output_file)

if __name__=="__main__":
    asyncio.run(main())
