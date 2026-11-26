import os, re, io, json, time, uuid, html, socket, hashlib, secrets, ipaddress, tempfile, threading, hmac
from urllib.parse import urljoin, urlparse

import requests
import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub
from readability import Document
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, send_file
from google import genai
from google.genai import types

load_dotenv()
BASE = os.path.dirname(os.path.abspath(__file__))
GLOS_PATH = os.path.join(BASE, "glosarium.json")

# Model default untuk masing-masing provider (bisa ditimpa via .env)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
# GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-r1:free").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()

PASSWORD = os.getenv("APP_PASSWORD", "").strip()
PAUSE = float(os.getenv("REQUEST_PAUSE", "4"))  # jeda antar panggilan API (detik)
CHUNK = 2500  # batas karakter per permintaan

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
app.secret_key = os.getenv("SECRET_KEY") or (
    hashlib.sha256(("k" + PASSWORD).encode()).hexdigest() if PASSWORD else secrets.token_hex(16))
if os.getenv("SPACE_ID"):  # di Hugging Face Spaces (tampil dalam iframe)
    app.config.update(SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=True)

SYSTEM = """Kamu adalah editor dan penerjemah sastra profesional spesialis Light Novel (Jepang) dan Web Novel (Inggris/Mandarin) ke Bahasa Indonesia. Terjemahkan teks menjadi bacaan yang mengalir alami, emosional, dan enak dibaca layaknya novel terjemahan resmi.
ATURAN WAJIB:
1. Gunakan Bahasa Indonesia baku yang luwes untuk narasi (tidak kaku seperti terjemahan mesin, tidak pula bahasa gaul berlebihan).
2. Sesuaikan kata ganti dialog antar karakter berdasarkan kedekatan mereka (aku/kamu untuk teman akrab, saya/Anda atau gelar untuk hubungan formal, gue/lo HANYA jika karakter berandalan modern).
3. Pertahankan honorifik Jepang (-san, -kun, -chan, -sama, Senpai, Sensei) kecuali pada latar fantasi Eropa murni (gunakan Tuan, Nona, Yang Mulia). Terjemahkan onomatope secara kontekstual dan ubah kalimat pasif Inggris yang berbelit menjadi kalimat aktif Bahasa Indonesia yang natural.
4. Patuhi seluruh nama, istilah, dan catatan pada referensi glosarium JSON di bawah ini secara mutlak.
5. Pertahankan pemisahan paragraf dan format italics (*...*) untuk monolog batin. Langsung berikan hasil terjemahan tanpa kata pengantar atau catatan tambahan."""

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}

# ------------------------------------------------------------------ auth
@app.before_request
def guard():
    if not PASSWORD or request.endpoint in ("login", "static"):
        return
    if not session.get("ok"):
        if request.path.startswith("/api/"):
            return jsonify(error="Sesi habis, silakan login lagi."), 401
        return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return redirect("/")
    err = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), PASSWORD):
            session["ok"] = True
            session.permanent = True
            return redirect("/")
        err = "Kata sandi salah."
    return render_template("index.html", login=True, error=err)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if PASSWORD else "/")

def has_any_env_key():
    return any(bool(os.getenv(k, "").strip()) for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"))

@app.route("/")
def index():
    return render_template("index.html", login=False, has_pw=bool(PASSWORD),
                           env_key=has_any_env_key())

def err(msg, code=400):
    return jsonify(error=msg), code

# ------------------------------------------------------------------ glosarium
EMPTY_GLOS = {"metadata_novel": {}, "karakter": {}, "istilah_dan_jurus": {}, "lokasi": {}, "aturan_khusus_bab": []}

def load_glos():
    try:
        with open(GLOS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(EMPTY_GLOS)

def save_glos(d):
    with open(GLOS_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

@app.get("/api/glosarium")
def glos_get():
    return jsonify(load_glos())

@app.put("/api/glosarium")
def glos_put():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return err("Format glosarium tidak valid.")
    save_glos(d)
    return jsonify(ok=True)

@app.get("/api/glosarium/download")
def glos_dl():
    return send_file(io.BytesIO(json.dumps(load_glos(), ensure_ascii=False, indent=2).encode("utf-8")),
                     as_attachment=True, download_name="glosarium.json", mimetype="application/json")

@app.post("/api/glosarium/upload")
def glos_up():
    f = request.files.get("file")
    try:
        d = json.loads(f.read().decode("utf-8-sig"))
        assert isinstance(d, dict)
    except Exception:
        return err("Berkas bukan JSON glosarium yang valid.")
    save_glos(d)
    return jsonify(d)

# ------------------------------------------------------------------ teks & scraper
def blocks(root):
    """Ambil blok teks (is_heading, teks) dari elemen HTML, italic -> *...*"""
    for t in root.find_all(["i", "em"]):
        t.insert(0, "*"); t.append("*"); t.unwrap()
    for br in root.find_all("br"):
        br.replace_with("\n")
    out = []
    els = root.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6"])
    for el in els:
        head = el.name.startswith("h")
        for ln in el.get_text().split("\n"):
            ln = re.sub(r"\s+", " ", ln).strip()
            if ln:
                out.append((head, ln))
    if not out:
        for ln in root.get_text("\n").split("\n"):
            ln = re.sub(r"\s+", " ", ln).strip()
            if ln:
                out.append((False, ln))
    return out

def safe_url(u):
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("URL tidak valid (harus diawali http:// atau https://).")
    for info in socket.getaddrinfo(p.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Alamat URL tidak diizinkan.")

SELECTORS = [".entry-content", ".post-content", ".chapter-content", "#chapter-content", ".reading-content",
             ".post-body", "article"]
JUNK_SEL = ("script,style,noscript,iframe,nav,footer,aside,button,svg,ins,.sharedaddy,.share,.social,"
            ".comments,#comments,.comment-respond,#respond,.wp-block-buttons,.code-block,.adsbygoogle,.ads,"
            ".advertisement,.nav-links,.post-navigation,.related-posts,.jp-relatedposts,.wpcnt,.addtoany_share_save_container")
_W = r"(?:(?:previous|prev|next)(?:\s+(?:chapter|page))?|home|index|table of contents|sebelumnya|selanjutnya|daftar isi)"
JUNK_LINE = re.compile(rf"^\W*{_W}(?:\W+{_W})*\W*$", re.I)
NEXT_RE = re.compile(r"\b(next|selanjutnya|berikutnya|lanjut)\b|下一|次の|次へ|›|»", re.I)
PREV_RE = re.compile(r"\b(prev|previous|sebelumnya)\b|上一|前の|‹|«", re.I)

def find_next(soup, url):
    for t in soup.find_all(["a", "link"], rel=True):
        if "next" in [r.lower() for r in t.get("rel", [])] and t.get("href"):
            return urljoin(url, t["href"])
    best = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        txt = a.get_text(" ", strip=True)
        cls = " ".join(a.get("class", [])).lower()
        ok = (txt and len(txt) <= 40 and NEXT_RE.search(txt) and not PREV_RE.search(txt)) or \
             ("next" in cls and "prev" not in cls)
        full = urljoin(url, href)
        if ok and full.rstrip("/") != url.rstrip("/"):
            best.append((0 if re.search(r"chapter|bab|章|話", txt, re.I) else 1, full))
    return sorted(best, key=lambda x: x[0])[0][1] if best else None

def scrape(url):
    safe_url(url)
    h = dict(HEADERS)
    p = urlparse(url)
    h["Referer"] = f"{p.scheme}://{p.netloc}/"
    r = requests.get(url, headers=h, timeout=25)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    page = r.text
    nxt = find_next(BeautifulSoup(page, "lxml"), r.url)
    soup = BeautifulSoup(page, "lxml")
    t = soup.select_one("h1.entry-title, h1.post-title, h1")
    title = t.get_text(" ", strip=True) if t else ""
    for j in soup.select(JUNK_SEL):
        j.decompose()
    root = None
    for sel in SELECTORS:
        root = next((c for c in soup.select(sel) if len(c.get_text(strip=True)) > 500), None)
        if root:
            break
    if root is None:
        doc = Document(page)
        root = BeautifulSoup(doc.summary(), "lxml")
        title = title or doc.short_title()
    lines = [x for _, x in blocks(root) if not JUNK_LINE.match(x)]
    if lines and title and lines[0].strip() == title.strip():
        lines = lines[1:]
    if not lines:
        raise ValueError("Isi cerita tidak ditemukan di halaman ini.")
    return {"title": title, "text": "\n\n".join(lines), "next_url": nxt}

@app.post("/api/scrape")
def api_scrape():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    try:
        return jsonify(scrape(url))
    except requests.HTTPError as e:
        return err(f"Situs menolak permintaan (HTTP {e.response.status_code}).")
    except Exception as e:
        return err(str(e)[:300])

# ------------------------------------------------------------------ penerjemah + job (Multi-AI)
JOBS = {}

def chunks(text):
    out, cur, n = [], [], 0
    for ln in [l.strip() for l in text.split("\n") if l.strip()]:
        if n + len(ln) > CHUNK and cur:
            out.append("\n\n".join(cur)); cur, n = [], 0
        cur.append(ln); n += len(ln) + 2
    if cur:
        out.append("\n\n".join(cur))
    return out

def get_groq_model(key):
    """Ambil otomatis model aktif terbaik dari akun Groq"""
    env_model = os.getenv("GROQ_MODEL", "").strip()
    if env_model:
        return env_model
    try:
        r = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if r.ok:
            # Abaikan model audio (whisper) dan keamanan (guard)
            ids = [
                m["id"] for m in r.json().get("data", [])
                if not any(x in m["id"].lower() for x in ("whisper", "guard", "tts", "playai"))
            ]
            # Urutan prioritas model terbaik untuk terjemahan sastra
            priorities = (
                "llama-3.3-70b",
                "llama-4-maverick",
                "llama-3.1-70b",
                "qwen",
                "gpt-oss-120b",
                "llama-4-scout",
                "70b",
            )
            for pref in priorities:
                match = next((m for m in ids if pref in m.lower()), None)
                if match:
                    return match
            if ids:
                return ids[0]
    except Exception:
        pass
    return GROQ_MODEL  # fallback ke default di bagian atas app.py

def detect_provider(key):
    """Deteksi otomatis provider AI berdasarkan awalan API Key"""
    if key.startswith("gsk_"):
        return "groq", "https://api.groq.com/openai/v1/chat/completions", get_groq_model(key)
    if key.startswith("sk-or-"):
        return "openrouter", "https://openrouter.ai/api/v1/chat/completions", OPENROUTER_MODEL
    if key.startswith("sk-"):
        return "deepseek", "https://api.deepseek.com/chat/completions", DEEPSEEK_MODEL
    return "gemini", None, GEMINI_MODEL

def build_ctx(k, system):
    provider, endpoint, model = detect_provider(k)
    client = genai.Client(api_key=k) if provider == "gemini" else k
    return {"provider": provider, "endpoint": endpoint, "model": model, "client": client, "system": system}

def setup(key):
    # Kumpulkan semua API key yang tersedia (prioritas: input UI -> Gemini -> DeepSeek -> OpenRouter -> Groq)
    raw_keys = [
        (key or "").strip(),
        os.getenv("GEMINI_API_KEY", "").strip(),
        os.getenv("DEEPSEEK_API_KEY", "").strip(),
        os.getenv("OPENROUTER_API_KEY", "").strip(),
        os.getenv("GROQ_API_KEY", "").strip(),
    ]
    # Hapus yang kosong dan duplikat sambil mempertahankan urutan prioritas
    keys = list(dict.fromkeys(k for k in raw_keys if k))
    if not keys:
        raise ValueError("API Key AI belum diisi (menu Pengaturan atau file .env).")

    system = SYSTEM + "\n\nREFERENSI GLOSARIUM (JSON):\n" + json.dumps(load_glos(), ensure_ascii=False, indent=1)
    providers = [build_ctx(k, system) for k in keys]
    return {"providers": providers, "idx": 0}

def call_ai(ctx, text, prev_context=""):
    provider = ctx["provider"]
    
    # Tambahkan konteks akhir paragraf sebelumnya jika ada
    user_prompt = text
    if prev_context:
        user_prompt = (
            f"[KONTEKS TERJEMAHAN SEBELUMNYA - JANGAN DITERJEMAHKAN ULANG, HANYA SEBAGAI ACUAN GAYA BAHASA & SUDUT PANDANG]:\n"
            f"{prev_context}\n\n"
            f"[TEKS YANG HARUS DITERJEMAHKAN SEKARANG]:\n"
            f"{text}"
        )

    if provider == "gemini":
        r = ctx["client"].models.generate_content(
            model=ctx["model"],
            contents=user_prompt,
            config=types.GenerateContentConfig(system_instruction=ctx["system"], temperature=0.25),
        )
        return (r.text or "").strip()

    headers = {
        "Authorization": f"Bearer {ctx['client']}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "http://localhost:7860"
        headers["X-Title"] = "Novel Translator"

    payload = {
        "model": ctx["model"],
        "messages": [
            {"role": "system", "content": ctx["system"]},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.25,
    }
    resp = requests.post(ctx["endpoint"], headers=headers, json=payload, timeout=90)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return (data["choices"][0]["message"]["content"] or "").strip()

def tr(job, ctx, text):
    if job["calls"]:
        time.sleep(PAUSE)
    job["calls"] += 1
    providers = ctx["providers"]
    wait = 15

    for attempt in range(6):
        cur = providers[ctx["idx"]]
        pname = cur["provider"].upper()
        job["msg"] = f"{job['label']}[{pname}] Menerjemahkan bagian {job['done'] + 1}/{job['total']}"
        try:
            res = call_ai(cur, text)
            job["done"] += 1
            return res
        except Exception as e:
            s = str(e)
            is_limit = any(x in s for x in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "rate_limit"))
            # Jika provider saat ini sibuk/limit dan masih ada provider cadangan lain, langsung pindah
            if is_limit and len(providers) > 1:
                ctx["idx"] = (ctx["idx"] + 1) % len(providers)
                nxt_name = providers[ctx["idx"]]["provider"].upper()
                job["msg"] = f"{job['label']}[{pname}] Sibuk, beralih otomatis ke {nxt_name}…"
                time.sleep(2)
                continue
            if attempt < 5 and is_limit:
                job["msg"] = f"{job['label']}[{pname}] Kuota/server sibuk, mencoba lagi dalam {wait} detik…"
                time.sleep(wait)
                wait = min(wait * 2, 90)
                continue
            raise

def friendly(e):
    s = str(e)
    if "429" in s or "RESOURCE_EXHAUSTED" in s or "rate_limit" in s:
        return "Kuota API habis atau terlalu cepat (429). Tunggu sebentar atau ganti API Key provider lain."
    if any(x in s for x in ("API_KEY_INVALID", "API key not valid", "401", "403", "invalid_api_key")):
        return "API Key tidak valid atau tidak punya akses."
    return s[:300] or e.__class__.__name__

def start(kind, name, fn, *a):
    now = time.time()
    for k in [k for k, j in JOBS.items() if now - j["t"] > 7200]:
        p = JOBS.pop(k)["files"].get("epub")
        if p and os.path.exists(p):
            os.remove(p)
    jid = uuid.uuid4().hex
    job = dict(id=jid, t=now, status="running", done=0, total=0, msg="Memulai…", label="", text="",
               files={}, error="", kind=kind, calls=0,
               name=re.sub(r"[^\w\- ]+", "", name or "")[:60].strip() or "terjemahan")
    JOBS[jid] = job

    def run():
        try:
            fn(job, *a)
            job["status"] = "done"
        except Exception as e:
            job["status"], job["error"] = "error", friendly(e)
    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=jid)

def job_text(job, text, key):
    ctx = setup(key)
    parts = chunks(text)
    job["total"] = len(parts)
    job["text"] = "\n\n".join(tr(job, ctx, p) for p in parts)

def md(s):
    s = html.escape(re.sub(r"^#+\s*", "", s), quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)

def job_epub(job, path, key):
    ctx = setup(key)
    try:
        book = epub.read_epub(path, {"ignore_ncx": True})
    finally:
        os.remove(path)
    chs = []
    for idref, _ in book.spine:  # urutan bab asli
        it = book.get_item_with_id(idref)
        if not it or it.get_type() != ebooklib.ITEM_DOCUMENT or isinstance(it, epub.EpubNav):
            continue
        body = BeautifulSoup(it.get_content(), "lxml").find("body")
        if not body:
            continue
        total = len(body.get_text(strip=True))
        links = sum(len(a.get_text(strip=True)) for a in body.find_all("a"))
        if total < 200 or links > 0.6 * total:  # halaman kosong / daftar isi
            continue
        bl = blocks(body)
        if bl:
            chs.append(bl)
    if not chs:
        raise ValueError("Tidak ada bab yang dapat dibaca di EPUB ini.")
    plans = [chunks("\n".join(t for _, t in bl)) for bl in chs]
    job["total"] = sum(len(p) for p in plans)

    meta = lambda k: (book.get_metadata("DC", k) or [("", {})])[0][0]
    out = epub.EpubBook()
    out.set_identifier(uuid.uuid4().hex)
    out.set_title((meta("title") or job["name"]) + " (Terjemahan Indonesia)")
    out.set_language("id")
    if meta("creator"):
        out.add_author(meta("creator"))
    items, txt = [], []
    for i, (bl, parts) in enumerate(zip(chs, plans), 1):
        job["label"] = f"Bab {i}/{len(chs)} · "
        res = "\n\n".join(tr(job, ctx, p) for p in parts)
        lines = [l.strip() for l in res.split("\n") if l.strip()] or ["(Bagian ini kosong.)"]
        head = bl[0][0]
        c = epub.EpubHtml(title=(re.sub(r"^#+\s*|\*", "", lines[0])[:80] if head else f"Bab {i}"),
                          file_name=f"bab_{i:03d}.xhtml", lang="id")
        c.content = "".join(f"<h2>{md(l)}</h2>" if (j == 0 and head) else f"<p>{md(l)}</p>"
                            for j, l in enumerate(lines))
        out.add_item(c); items.append(c); txt.append("\n\n".join(lines))
        job["text"] = "\n\n\n".join(txt)
    out.toc = tuple(items)
    out.add_item(epub.EpubNcx()); out.add_item(epub.EpubNav())
    out.spine = ["nav"] + items
    dest = os.path.join(tempfile.gettempdir(), job["id"] + "_out.epub")
    epub.write_epub(dest, out)
    job["files"]["epub"] = dest
    job["msg"] = f"Selesai: {len(items)} bab diterjemahkan."

@app.post("/api/job/text")
def job_text_api():
    d = request.get_json(silent=True) or {}
    text = (d.get("text") or "").strip()
    if not text:
        return err("Teks kosong.")
    return start("text", d.get("title"), job_text, text, d.get("key"))

def decode(b):
    for enc in ("utf-8-sig", "cp932", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("latin-1")

@app.post("/api/job/file")
def job_file_api():
    f, key = request.files.get("file"), request.form.get("key", "")
    if not f or not f.filename:
        return err("Pilih berkas dulu.")
    name, _, ext = f.filename.rpartition(".")
    ext = ext.lower()
    if ext == "epub":
        tmp = os.path.join(tempfile.gettempdir(), uuid.uuid4().hex + ".epub")
        f.save(tmp)
        return start("epub", name, job_epub, tmp, key)
    if ext == "txt":
        text = decode(f.read()).strip()
        if not text:
            return err("Berkas kosong.")
        return start("text", name, job_text, text, key)
    return err("Hanya berkas .epub dan .txt yang didukung.")

@app.get("/api/job/<jid>")
def job_poll(jid):
    j = JOBS.get(jid)
    if not j:
        return err("Tugas tidak ditemukan.", 404)
    d = {k: j[k] for k in ("status", "done", "total", "msg", "error", "kind", "name")}
    d["has_epub"] = "epub" in j["files"]
    if j["status"] == "done" and j["kind"] == "text":
        d["text"] = j["text"]
    return jsonify(d)

@app.get("/api/job/<jid>/download/<fmt>")
def job_dl(jid, fmt):
    j = JOBS.get(jid)
    if not j or j["status"] != "done":
        return err("Berkas belum siap.", 404)
    if fmt == "epub" and "epub" in j["files"]:
        return send_file(j["files"]["epub"], as_attachment=True, download_name=j["name"] + "-ID.epub",
                         mimetype="application/epub+zip")
    if fmt == "txt":
        return send_file(io.BytesIO(j["text"].encode("utf-8")), as_attachment=True,
                         download_name=j["name"] + "-ID.txt", mimetype="text/plain")
    return err("Format tidak tersedia.", 404)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 7860)), threaded=True)