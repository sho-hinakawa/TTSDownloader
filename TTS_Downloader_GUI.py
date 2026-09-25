import sys
import os
import re
import csv
import time
import json
import hashlib
import zipfile
import shutil
import threading
import traceback
import locale

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

try:
    from PIL import Image
    from io import BytesIO
except ImportError:
    print("ERROR: pip install Pillow")
    sys.exit(1)

bson_decode = None
try:
    from bson import decode as bson_decode
except ImportError:
    print("ERROR: pip install pymongo")
    sys.exit(1)

try:
    import trimesh
except ImportError:
    print("ERROR: pip install trimesh")
    sys.exit(1)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

RETRIES = 3
DELAY_BETWEEN_DOWNLOADS = 0.15
MAX_WORKERS = 8
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LANG_FILE = os.path.join(SCRIPT_DIR, "languages.json")

mime_to_ext = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "application/pdf": ".pdf", "application/octet-stream": None, "model/obj": ".obj",
}
csv_lock = Lock()
file_lock = Lock()



def detect_system_language():
    """Devuelve 'es' o 'en' según el idioma del sistema."""
    candidates = []
    try:
        lang, _ = locale.getdefaultlocale()
        if lang:
            candidates.append(lang)
    except Exception:
        pass
    try:
        lang = locale.getlocale()[0]
        if lang:
            candidates.append(lang)
    except Exception:
        pass
    for key in ("LANG", "LC_ALL", "LC_MESSAGES", "LANGUAGE"):
        val = os.environ.get(key, "")
        if val:
            candidates.append(val.split(":")[0])
    for c in candidates:
        c = (c or "").replace("-", "_").lower()
        if c.startswith("es"):
            return "es"
        if c.startswith("en"):
            return "en"
    return "es"  # por defecto español


def load_languages():
    candidates = [
        LANG_FILE,
        os.path.join(os.getcwd(), "languages.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if "es" not in data or "en" not in data:
                raise ValueError("languages.json debe tener las claves 'es' y 'en'.")
            return data
    raise FileNotFoundError(
        f"No se encuentra languages.json\nBuscado en:\n{LANG_FILE}\n\n"
        "Pon languages.json en la misma carpeta que este script."
    )


# ====================== UTILIDADES COMUNES ======================

def get_extension_from_mime(response):
    ct = response.headers.get("Content-Type", "").lower().split(";")[0].strip()
    return mime_to_ext.get(ct) if ct else None


def is_valid_steam_workshop_url(url):
    if not url:
        return False
    patterns = [
        r"https?://steamcommunity\.com/sharedfiles/filedetails/\?id=\d+",
        r"https?://steamcommunity\.com/workshop/filedetails/\?id=\d+",
    ]
    return any(re.search(p, url, re.I) for p in patterns)


def extract_steam_id(url):
    m = re.search(r"id=(\d+)", url)
    return m.group(1) if m else None


def get_with_retries(url, headers=None, retries=RETRIES, initial_delay=DELAY_BETWEEN_DOWNLOADS):
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Referer": "https://steamcommunity.com/",
        }
    delay = initial_delay
    for attempt in range(retries):
        try:
            r = requests.get(url, stream=True, allow_redirects=True, timeout=25, headers=headers)
            if r.status_code == 404:
                raise requests.exceptions.RequestException("404")
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                if attempt == retries - 1:
                    raise requests.exceptions.RequestException("429")
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                raise requests.exceptions.RequestException(f"HTTP {r.status_code}")
        except requests.exceptions.RequestException as e:
            if "404" in str(e):
                raise
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 1.5
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
    raise requests.exceptions.RequestException("Connection error")


def clean_steam_url(url):
    if "cloud-3.steamusercontent.com" in url.lower():
        return url.replace("http://cloud-3.steamusercontent.com", "https://steamusercontent-a.akamaihd.net")
    return url


def clean_extracted_url(url):
    return re.sub(r"[\]\}\)\'\"\\].*$", "", url).strip().strip("\x00")


def collect_assets(data):
    decks, cards, others = {}, [], []
    seen_urls = set()

    def add_other(kind, url, label):
        if not url or not isinstance(url, str):
            return
        url = clean_extracted_url(url)
        if not url.startswith(("http://", "https://")):
            return
        if url in seen_urls:
            return
        seen_urls.add(url)
        others.append((kind, url, label))

    def walk_obj(o):
        if not isinstance(o, dict):
            return

        name = o.get("Name", "") or ""
        nick = (o.get("Nickname") or "").strip()
        label = nick or name or "unnamed"

        # Decks
        if isinstance(o.get("CustomDeck"), dict):
            for k, v in o["CustomDeck"].items():
                if k not in decks and isinstance(v, dict):
                    decks[k] = v

        # Cards
        if name == "Card" and "CardID" in o:
            cid = o["CardID"]
            dk = next(iter(o["CustomDeck"])) if o.get("CustomDeck") else str(cid // 100)
            cards.append((cid, nick, dk))

        # Custom
        img = o.get("CustomImage")
        if isinstance(img, dict):
            if name in ("Custom_Token", "Custom_Token_Stack", "Figurine_Custom"):
                kind = "token"
            elif name in ("Custom_Tile", "Tile", "Custom_Board"):
                kind = "image"
            else:
                kind = "token" if name.startswith("Custom_Token") else "image"
            for key in ("ImageURL", "ImageSecondaryURL"):
                url = img.get(key)
                if url:
                    add_other(kind, url, f"{label}_{key}" if key != "ImageURL" else label)

        # CustomMesh / modelos
        mesh = o.get("CustomMesh")
        if isinstance(mesh, dict) or name in (
            "Custom_Model", "Custom_Model_Bag", "Custom_Model_Stack"
        ):
            mesh = mesh or {}
            for key in ("DiffuseURL", "NormalURL", "MeshURL", "ColliderURL"):
                url = mesh.get(key)
                if url:
                    add_other("model", url, f"{label}_{key}")

        # PDF
        if name == "Custom_PDF" or isinstance(o.get("CustomPDF"), dict):
            pdf = o.get("CustomPDF") or {}
            url = pdf.get("PDFUrl")
            if url:
                add_other("pdf", url, label)

        # Assetbundles (Unity)
        if name == "Custom_Assetbundle" or isinstance(o.get("CustomAssetbundle"), dict):
            ab = o.get("CustomAssetbundle") or {}
            for key in ("AssetbundleURL", "AssetbundleSecondaryURL"):
                url = ab.get(key)
                if url:
                    add_other("assetbundle", url, f"{label}_{key}" if key != "AssetbundleURL" else label)

        for key in ("ContainedObjects", "ChildObjects"):
            children = o.get(key)
            if isinstance(children, list):
                for child in children:
                    walk_obj(child)

        # States: dict de estados alternativos del objeto
        states = o.get("States")
        if isinstance(states, dict):
            for st in states.values():
                walk_obj(st)

    for obj in (data.get("ObjectStates") or []):
        walk_obj(obj)

    return decks, cards, others


def crop_card(sheet, index, nw, nh):
    w, h = sheet.size
    cw, ch = w // nw, h // nh
    col, row = index % nw, index // nw
    return sheet.crop((col * cw, row * ch, (col + 1) * cw, (row + 1) * ch))


def open_image_from_cache(url, sheet_cache):
    entry = sheet_cache.get(url) or sheet_cache.get(clean_steam_url(url))
    if not entry:
        return None
    path, content = entry
    try:
        return Image.open(BytesIO(content)).convert("RGBA") if content else Image.open(path).convert("RGBA")
    except Exception:
        return None


def unique_path(directory, filename):
    out = os.path.join(directory, filename)
    if not os.path.exists(out):
        return out, filename
    base, ext = os.path.splitext(filename)
    k = 1
    while True:
        name = f"{base}_{k}{ext}"
        out = os.path.join(directory, name)
        if not os.path.exists(out):
            return out, name
        k += 1


def get_file_extension_from_url(url):
    if not url:
        return None
    u = url.lower()
    last, pos = None, -1
    for ext in [".jpg", ".jpeg", ".png", ".pdf", ".obj"]:
        p = u.rfind(ext)
        if p > pos:
            pos, last = p, ext
    return last


def get_extension_from_response(response):
    cd = response.headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        ext = os.path.splitext(m.group(1))[1].lower()
        if ext:
            return ext
    return None


def verify_header_signature(content):
    try:
        if content.startswith(b"\xFF\xD8\xFF"):
            return ".jpg"
        if content.startswith(b"\x89PNG"):
            return ".png"
        if content.startswith(b"%PDF"):
            return ".pdf"
        text = content[:512].decode("ascii", errors="ignore").strip()
        if text.startswith("#") or any(
            line.startswith(("v ", "f ", "vt ", "vn ", "o ", "mtllib")) for line in text.splitlines()[:10]
        ):
            return ".obj"
    except Exception:
        pass
    return None


def determine_final_extension(field, url_ext, resp_ext, mime_ext, sig_ext):
    fl = (field or "").lower()
    if "meshurl" in fl or "colliderurl" in fl or "modelurl" in fl:
        if sig_ext in (".obj",) or not url_ext:
            return ".obj"
        if url_ext in (".obj", ".stl"):
            return url_ext.lower()
        return ".obj"
    if "pdfurl" in fl:
        return ".pdf"
    if "assetbundle" in fl:
        return ".unity3d"
    if url_ext:
        return url_ext.lower()
    if resp_ext:
        return resp_ext.lower()
    if mime_ext:
        return mime_ext.lower()
    if sig_ext:
        return sig_ext.lower()
    return ".junk"


def should_download(ext):
    return ext in {".jpg", ".jpeg", ".png", ".pdf", ".obj"}


def _looks_like_obj_file(path):
    if path.lower().endswith(".obj"):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
        text = head.decode("ascii", errors="ignore").strip()
        if not text:
            return False
        lines = text.splitlines()[:15]
        return any(
            line.startswith(("v ", "vn ", "vt ", "f ", "o ", "g ", "mtllib ", "usemtl ", "#"))
            for line in lines
        ) and any(line.startswith(("v ", "f ")) for line in lines)
    except Exception:
        return False


def convert_obj_to_stl(obj_path):
    try:
        loaded = trimesh.load(obj_path, file_type="obj", force="mesh")
        if isinstance(loaded, trimesh.Scene):
            geoms = [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
            if not geoms:
                return False, "escena sin mallas triangulares"
            mesh = trimesh.util.concatenate(geoms) if len(geoms) > 1 else geoms[0]
        elif isinstance(loaded, trimesh.Trimesh):
            mesh = loaded
        else:
            return False, f"tipo no soportado: {type(loaded).__name__}"
        if mesh is None or (hasattr(mesh, "faces") and len(mesh.faces) == 0):
            return False, "malla vacía"
        base = os.path.splitext(obj_path)[0]
        stl_path = base + ".stl"
        mesh.export(stl_path)
        return True, stl_path
    except Exception as e:
        return False, str(e)[:160]


def convert_models_folder(model_dir, log_fn=None, t_fn=None):
    if not os.path.isdir(model_dir):
        if log_fn and t_fn:
            log_fn(t_fn("stl_no_models"))
        return 0, 0

    candidates = []
    for n in os.listdir(model_dir):
        full = os.path.join(model_dir, n)
        if not os.path.isfile(full):
            continue
        low = n.lower()
        if low.endswith((".stl", ".jpg", ".jpeg", ".png", ".pdf", ".mtl", ".unity3d", ".junk")):
            continue
        if _looks_like_obj_file(full):
            candidates.append(full)

    if not candidates:
        if log_fn and t_fn:
            log_fn(t_fn("stl_no_obj"))
        return 0, 0

    ok_c = fail_c = 0
    if log_fn and t_fn:
        log_fn(t_fn("stl_header"))
        log_fn(t_fn("stl_found").format(len(candidates)))
    for obj_path in candidates:
        name = os.path.basename(obj_path)
        ok, result = convert_obj_to_stl(obj_path)
        if ok:
            ok_c += 1
            if log_fn and t_fn:
                log_fn(t_fn("stl_ok").format(os.path.basename(result)))
        else:
            fail_c += 1
            if log_fn and t_fn:
                log_fn(t_fn("stl_fail").format(name, result))
    return ok_c, fail_c


def remove_empty_dirs(root):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def get_unique_download_path(base):
    if not os.path.exists(base):
        return base
    n = 2
    while True:
        p = f"{base}_{n}"
        if not os.path.exists(p):
            return p
        n += 1


def safe_label(text, max_len=40):
    if not text:
        return "unnamed"
    c = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in str(text))
    return c[:max_len].strip("_") or "unnamed"


def make_filename(field, url, ext):
    h = hashlib.md5(url.encode()).hexdigest()[:10]
    return f"{safe_label(field, 50)}_{h}{ext}"


def download_file(url, download_path, field_name, log_fn=None, t_fn=None):
    try:
        clean_url = clean_steam_url(url)
        response = get_with_retries(clean_url)
        content = response.content
        url_ext = get_file_extension_from_url(clean_url)
        resp_ext = get_extension_from_response(response)
        mime_ext = get_extension_from_mime(response)
        sig_ext = verify_header_signature(content[:1024])
        final_ext = determine_final_extension(field_name, url_ext, resp_ext, mime_ext, sig_ext)
        if not should_download(final_ext):
            if log_fn and t_fn:
                log_fn(t_fn("omitted").format(field_name))
            return "omitted", field_name, "", None
        filename = make_filename(field_name, clean_url, final_ext)
        path = os.path.join(download_path, filename)
        with file_lock:
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(content)
        if log_fn and t_fn:
            log_fn(t_fn("ok_file").format(filename))
        return "success", field_name, filename, content
    except Exception as e:
        if log_fn and t_fn:
            log_fn(t_fn("fail_file").format(field_name, str(e)[:100]))
        return "failed", field_name, "", None


def init_csv(path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=";").writerow(["Campo", "URL", "Archivo"])


def append_to_csv(path, field, url, filename=""):
    with csv_lock:
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            csv.writer(f, delimiter=";").writerow([field, url, filename])


# ====================== GUI ======================

class App(tk.Tk):
    def __init__(self, languages):
        super().__init__()
        self.languages = languages
        self.lang = detect_system_language()
        self.cancel_flag = threading.Event()
        self.worker = None
        self.download_path = None
        self._log_lines = []
        self.json_path = tk.StringVar()
        self.url_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="json")
        self._suppress_input_clear = False

        self.title(self.t("app_title"))
        self.geometry("820x620")
        self.minsize(700, 500)
        self.update_idletasks()
        w, h = 820, 620
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        try:
            style = ttk.Style()
            style.theme_use("clam")
        except Exception:
            pass

        self._build()
        self._apply_language()
        self._on_mode()
        self.json_path.trace_add("write", self._on_input_changed)
        self.url_var.trace_add("write", self._on_input_changed)

    def t(self, key):
        return self.languages.get(self.lang, {}).get(key, key)

    def _build(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Idioma
        top = ttk.Frame(main)
        top.pack(fill=tk.X, pady=(0, 6))
        self.lbl_lang = ttk.Label(top, text="")
        self.lbl_lang.pack(side=tk.LEFT)
        self.lang_var = tk.StringVar(value="Español" if self.lang == "es" else "English")
        self.cmb_lang = ttk.Combobox(
            top, textvariable=self.lang_var, values=["Español", "English"],
            state="readonly", width=12
        )
        self.cmb_lang.pack(side=tk.LEFT, padx=(6, 0))
        self.cmb_lang.bind("<<ComboboxSelected>>", self._on_lang)
        ttk.Button(top, text="?", width=3, command=self._about).pack(side=tk.RIGHT)

        # Modo
        mode_row = ttk.Frame(main)
        mode_row.pack(fill=tk.X, pady=(0, 6))
        self.lbl_mode = ttk.Label(mode_row, text="")
        self.lbl_mode.pack(side=tk.LEFT)
        self.rb_json = ttk.Radiobutton(
            mode_row, text="", variable=self.mode_var, value="json", command=self._on_mode
        )
        self.rb_json.pack(side=tk.LEFT, padx=(8, 0))
        self.rb_ws = ttk.Radiobutton(
            mode_row, text="", variable=self.mode_var, value="workshop", command=self._on_mode
        )
        self.rb_ws.pack(side=tk.LEFT, padx=(8, 0))

        # Entrada JSON
        self.frm_json = ttk.LabelFrame(main, padding=8)
        self.frm_json.pack(fill=tk.X, pady=(0, 4))
        self.lbl_json = ttk.Label(self.frm_json, text="")
        self.lbl_json.pack(anchor=tk.W)
        row_j = ttk.Frame(self.frm_json)
        row_j.pack(fill=tk.X, pady=(4, 0))
        self.ent_json = ttk.Entry(row_j, textvariable=self.json_path)
        self.ent_json.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.btn_browse = ttk.Button(row_j, text="", command=self._browse)
        self.btn_browse.pack(side=tk.LEFT, padx=(6, 0))

        # Entrada Workshop
        self.frm_ws = ttk.LabelFrame(main, padding=8)
        self.frm_ws.pack(fill=tk.X, pady=(0, 4))
        self.lbl_url = ttk.Label(self.frm_ws, text="")
        self.lbl_url.pack(anchor=tk.W)
        self.ent_url = ttk.Entry(self.frm_ws, textvariable=self.url_var)
        self.ent_url.pack(fill=tk.X, pady=(4, 0))
        self.ent_url.bind("<Return>", lambda e: self._start())

        # Botones
        self.btn_row = ttk.Frame(main)
        self.btn_row.pack(fill=tk.X, pady=(4, 6))
        self.btn_start = ttk.Button(self.btn_row, text="", command=self._start)
        self.btn_start.pack(side=tk.LEFT)
        self.btn_cancel = ttk.Button(self.btn_row, text="", command=self._cancel, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_open = ttk.Button(self.btn_row, text="", command=self._open_folder, state=tk.DISABLED)
        self.btn_open.pack(side=tk.LEFT, padx=(8, 0))

        self.progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X, pady=(0, 2))
        self.lbl_status = ttk.Label(main, text="")
        self.lbl_status.pack(anchor=tk.W, pady=(0, 4))
        self._progress_total = 0
        self._progress_done = 0

        self.frm_log = ttk.LabelFrame(main, padding=6)
        self.frm_log.pack(fill=tk.BOTH, expand=True)
        self.txt_log = scrolledtext.ScrolledText(
            self.frm_log, wrap=tk.WORD, height=16, font=("Consolas", 9), state=tk.DISABLED
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True)
        self.txt_log.tag_configure("ok", foreground="#1a7a1a")
        self.txt_log.tag_configure("err", foreground="#c0392b")
        self.txt_log.tag_configure("warn", foreground="#b9770e")
        self.txt_log.tag_configure("info", foreground="#1a5276")
        self.txt_log.tag_configure("header", foreground="#2c3e50", font=("Consolas", 9, "bold"))

        self.lift()
        try:
            self.attributes("-topmost", True)
            self.after(400, lambda: self.attributes("-topmost", False))
        except Exception:
            pass

    def _apply_language(self):
        t = self.t
        self.title(t("app_title"))
        self.lbl_lang.config(text=t("language"))
        self.lbl_mode.config(text=t("mode_label"))
        self.rb_json.config(text=t("mode_json"))
        self.rb_ws.config(text=t("mode_workshop"))
        self.frm_json.config(text=t("select_json"))
        self.lbl_json.config(text=t("select_json"))
        self.btn_browse.config(text=t("browse"))
        self.frm_ws.config(text=t("label_url"))
        self.lbl_url.config(text=t("label_url"))
        self.btn_start.config(text=t("start"))
        self.btn_cancel.config(text=t("cancel"))
        self.btn_open.config(text=t("open_folder"))
        self.frm_log.config(text=t("log"))
        if not self.worker or not self.worker.is_alive():
            self.lbl_status.config(text=t("status_ready"))

    def _on_lang(self, event=None):
        self.lang = "es" if self.lang_var.get() == "Español" else "en"
        self._apply_language()

    def _on_mode(self):
        self.frm_json.pack_forget()
        self.frm_ws.pack_forget()
        if self.mode_var.get() == "json":
            self.frm_json.pack(fill=tk.X, pady=(0, 4), before=self.btn_row)
        else:
            self.frm_ws.pack(fill=tk.X, pady=(0, 4), before=self.btn_row)
        self._suppress_input_clear = True
        try:
            self.url_var.set("")
            self.json_path.set("")
        finally:
            self._suppress_input_clear = False
        self._clear_log_ui(reset_progress=True)

    def _on_input_changed(self, *_args):
        """Limpia el registro al seleccionar un nuevo JSON o escribir una nueva URL."""
        if self._suppress_input_clear:
            return
        if self.worker and self.worker.is_alive():
            return
        self._clear_log_ui(reset_progress=True)

    def _clear_log_ui(self, reset_progress=False):
        """Vacía el área de registro y, opcionalmente, progreso/estado."""
        has_log = bool(self._log_lines) or bool(self.txt_log.get("1.0", "end-1c").strip())
        self._log_lines = []
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.config(state=tk.DISABLED)
        if reset_progress and (has_log or self.download_path is not None):
            self.progress["value"] = 0
            self.lbl_status.config(text=self.t("status_ready"))
            self.btn_open.config(state=tk.DISABLED)
            self.download_path = None
            self._progress_total = 0
            self._progress_done = 0

    def _about(self):
        messagebox.showinfo(self.t("about"), self.t("about_text"))

    def _browse(self):
        path = filedialog.askopenfilename(
            title=self.t("select_json"),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.json_path.set(path)  

    def _log(self, msg, tag=None):
        self._log_lines.append(msg)
        def _do():
            self.txt_log.config(state=tk.NORMAL)
            if tag:
                self.txt_log.insert(tk.END, msg + "\n", tag)
            else:
                if msg.startswith("  ✓") or "✓" in msg[:6]:
                    self.txt_log.insert(tk.END, msg + "\n", "ok")
                elif msg.startswith("  ✗") or "✗" in msg[:6]:
                    self.txt_log.insert(tk.END, msg + "\n", "err")
                elif msg.startswith("  ⚠") or "⚠" in msg[:6]:
                    self.txt_log.insert(tk.END, msg + "\n", "warn")
                elif msg.startswith("==="):
                    self.txt_log.insert(tk.END, msg + "\n", "header")
                else:
                    self.txt_log.insert(tk.END, msg + "\n")
            self.txt_log.see(tk.END)
            self.txt_log.config(state=tk.DISABLED)
        self.after(0, _do)

    def _set_status(self, text):
        self.after(0, lambda: self.lbl_status.config(text=text))

    def _set_busy(self, busy):
        def _do():
            st = tk.DISABLED if busy else tk.NORMAL
            self.btn_start.config(state=st)
            self.btn_cancel.config(state=tk.NORMAL if busy else tk.DISABLED)
            self.btn_browse.config(state=st)
            self.cmb_lang.config(state=tk.DISABLED if busy else "readonly")
            self.rb_json.config(state=st)
            self.rb_ws.config(state=st)
            self.ent_json.config(state=st)
            self.ent_url.config(state=st)
            if busy:
                self.progress["value"] = 0
            else:
                if self.download_path and os.path.isdir(self.download_path):
                    self.btn_open.config(state=tk.NORMAL)
                    self.progress["value"] = 100
        self.after(0, _do)

    def _reset_progress(self, total):
        self._progress_total = max(total, 1)
        self._progress_done = 0
        self.after(0, lambda: self.progress.configure(value=0, maximum=100))

    def _tick_progress(self, n=1, status_text=None):
        self._progress_done += n
        pct = min(100, int(100 * self._progress_done / self._progress_total))
        def _ui():
            self.progress["value"] = pct
            if status_text is not None:
                self.lbl_status.config(text=f"{status_text}  ({pct}%)")
        self.after(0, _ui)

    def _set_progress_pct(self, pct, status_text=None):
        pct = max(0, min(100, int(pct)))
        def _ui():
            self.progress["value"] = pct
            if status_text is not None:
                self.lbl_status.config(text=f"{status_text}  ({pct}%)")
        self.after(0, _ui)

    def _start(self):
        t = self.t
        mode = self.mode_var.get()

        if mode == "json":
            path = self.json_path.get().strip().strip('"').strip("'")
            if not path:
                messagebox.showwarning(t("error_title"), t("no_file"))
                return
            if not os.path.isfile(path):
                messagebox.showerror(t("error_title"), t("file_not_found").format(path))
                return
            arg = path
        else:
            url = self.url_var.get().strip()
            if not is_valid_steam_workshop_url(url):
                messagebox.showerror(t("err_invalid_url_title"), t("err_invalid_url_msg"))
                return
            if bson_decode is None:
                messagebox.showerror(
                    t("error_title"),
                    "pip install pymongo\n(necesario para modo Workshop)",
                )
                return
            arg = url

        if self.worker and self.worker.is_alive():
            return

        self.cancel_flag.clear()
        self._log_lines = []
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.config(state=tk.DISABLED)
        self.btn_open.config(state=tk.DISABLED)
        self.download_path = None
        self._reset_progress(1)
        self._set_busy(True)
        self.worker = threading.Thread(
            target=self._run, args=(mode, arg), daemon=True
        )
        self.worker.start()

    def _cancel(self):
        if messagebox.askyesno(self.t("cancel"), self.t("confirm_cancel")):
            self.cancel_flag.set()
            self._set_status(self.t("status_cancelled"))

    def _open_folder(self):
        if self.download_path and os.path.isdir(self.download_path):
            path = os.path.abspath(self.download_path)
            try:
                if os.name == "nt":
                    os.startfile(path)
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", path])
            except Exception as e:
                messagebox.showerror(self.t("error_title"), str(e))

    def _run(self, mode, arg):
        t = self.t
        log = self._log
        try:
            if mode == "json":
                self._pipeline_json(arg, t, log)
            else:
                self._pipeline_workshop(arg, t, log)
        except Exception as e:
            log(f"ERROR: {e}", "err")
            log(traceback.format_exc(), "err")
            self._set_status(t("status_error"))
            self.after(0, lambda: messagebox.showerror(t("error_title"), str(e)))
        finally:
            self._set_busy(False)

    def _pipeline_workshop(self, workshop_url, t, log):
        workshop_id = extract_steam_id(workshop_url)
        if not workshop_id:
            log(t("log_id_error"), "err")
            self._set_status(t("status_error"))
            return

        log(t("log_workshop_id").format(workshop_id), "info")
        log(t("log_getting_info"), "info")
        self._set_progress_pct(5, t("status_working"))

        api_url = (
            "https://www.steamworkshopdownloader.cc/json?"
            f"url=https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"
        )
        data = requests.get(api_url, timeout=30).json()
        workshop_title = data.get("title", "WorkshopItem")
        download_url = data.get("download_url")
        log(t("log_title").format(workshop_title), "info")
        self._set_progress_pct(10, t("status_working"))

        safe = re.sub(r'[<>:"/\\|?*\s]', "_", workshop_title)
        out_base = os.getcwd()
        download_path = get_unique_download_path(os.path.join(out_base, safe))
        os.makedirs(download_path, exist_ok=True)
        self.download_path = download_path
        log(t("saving_to").format(download_path), "info")

        log(t("log_downloading_workshop"), "info")
        self._set_progress_pct(15, t("log_downloading_workshop"))
        bson_content = get_with_retries(download_url).content
        self._set_progress_pct(25, t("log_bson_to_json"))

        log(t("log_bson_to_json"), "info")
        try:
            json_data = bson_decode(bson_content)
            log(t("log_bson_ok"), "info")
        except Exception as e:
            log(t("log_bson_error").format(e), "err")
            self._set_status(t("status_error"))
            return

        if self.cancel_flag.is_set():
            self._set_status(t("status_cancelled"))
            return

        self._process_assets(json_data, download_path, safe, t, log, source_label=workshop_url)

    def _pipeline_json(self, json_input, t, log):
        out_base = os.path.dirname(os.path.abspath(json_input)) or os.getcwd()
        test_file = os.path.join(out_base, ".write_test.tmp")
        try:
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
        except PermissionError:
            self.after(0, lambda: messagebox.showerror(t("error_title"), t("no_write")))
            self._set_status(t("status_error"))
            return

        self._set_status(t("status_reading"))
        log(f"JSON: {json_input}")

        try:
            with open(json_input, encoding="utf-8") as f:
                json_data = json.load(f)
        except Exception as e:
            self.after(0, lambda: messagebox.showerror(t("error_title"), t("json_error").format(e)))
            self._set_status(t("status_error"))
            return

        if not isinstance(json_data, dict) or "SaveName" not in json_data:
            self.after(0, lambda: messagebox.showerror(
                t("error_title"), t("not_tts_json")
            ))
            self._set_status(t("status_error"))
            log(t("not_tts_json"), "err")
            return

        if self.cancel_flag.is_set():
            self._set_status(t("status_cancelled"))
            return

        save_name = json_data.get("SaveName") or os.path.splitext(os.path.basename(json_input))[0]
        safe = re.sub(r'[<>:"/\\|?*\s]', "_", str(save_name))
        download_path = get_unique_download_path(os.path.join(out_base, safe))
        os.makedirs(download_path, exist_ok=True)
        self.download_path = download_path
        log(t("saving_to").format(download_path), "info")

        self._process_assets(json_data, download_path, safe, t, log, source_label=json_input)

    def _process_assets(self, json_data, download_path, safe_name, t, log, source_label=""):
        sheets_dir = os.path.join(download_path, "sheets")
        cards_dir = os.path.join(download_path, "cards")
        token_dir = os.path.join(download_path, "token")
        model_dir = os.path.join(download_path, "model")
        pdf_dir = os.path.join(download_path, "pdf")
        image_dir = os.path.join(download_path, "image")
        assetbundle_dir = os.path.join(download_path, "assetbundle")
        for d in (sheets_dir, cards_dir, token_dir, model_dir, pdf_dir, image_dir, assetbundle_dir):
            os.makedirs(d, exist_ok=True)
        kind_to_dir = {
            "token": token_dir, "model": model_dir, "pdf": pdf_dir,
            "image": image_dir, "assetbundle": assetbundle_dir,
        }

        self._set_status(t("status_analyzing"))
        decks, cards, others = collect_assets(json_data)
        log(t("decks_found").format(len(decks)))
        log(t("cards_found").format(len(cards)))
        log(t("others_found").format(len(others)))

        csv_path = os.path.join(download_path, f"{safe_name}_urls.csv")
        init_csv(csv_path)
        successful = failed = skipped = cropped_front = cropped_back = 0

        if self.cancel_flag.is_set():
            self._set_status(t("status_cancelled"))
            return

        self._set_status(t("status_downloading_sheets"))
        log(t("sheets_header").format(MAX_WORKERS), "header")
        sheet_cache = {}
        tasks = []
        for did, info in decks.items():
            for side in ("FaceURL", "BackURL"):
                url = info.get(side)
                if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                    tasks.append((f"deck{did}_{side[:-3].lower()}", clean_extracted_url(url)))
        seen, unique = set(), []
        for field, url in tasks:
            if url not in seen:
                seen.add(url)
                unique.append((field, url))

        unique_card_count = len({cid for cid, _, _ in cards})
        other_unique = set()
        for kind, url, label in others:
            other_unique.add(clean_extracted_url(url))
        total_steps = len(unique) + unique_card_count + len(other_unique)
        self._reset_progress(max(total_steps, 1))
        phase_sheets = t("status_downloading_sheets")

        def _dl_sheet(args):
            if self.cancel_flag.is_set():
                return None
            field, url = args
            return field, url, download_file(url, sheets_dir, field, log, t)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for fut in as_completed([ex.submit(_dl_sheet, x) for x in unique]):
                if self.cancel_flag.is_set():
                    break
                result = fut.result()
                if result is None:
                    continue
                field, url, (status, _, filename, content) = result
                append_to_csv(csv_path, field, url, filename or "")
                if status == "success":
                    successful += 1
                    local = os.path.join(sheets_dir, filename)
                    sheet_cache[url] = (local, content)
                    sheet_cache[clean_steam_url(url)] = (local, content)
                elif status == "omitted":
                    skipped += 1
                else:
                    failed += 1
                self._tick_progress(1, phase_sheets)

        if self.cancel_flag.is_set():
            self._set_status(t("status_cancelled"))
            return

        # Cards
        phase_cards = t("status_cropping")
        self._set_status(phase_cards)
        log(t("cards_header"), "header")
        seen_cids = set()
        for cid, nick, did in cards:
            if self.cancel_flag.is_set():
                break
            if cid in seen_cids:
                continue
            seen_cids.add(cid)
            if did not in decks:
                log(t("deck_warn").format(did, cid), "warn")
                continue
            info = decks[did]
            index = cid % 100
            nw, nh = info.get("NumWidth", 10), info.get("NumHeight", 7)
            unique_back = bool(info.get("UniqueBack", False))
            base = f"card_{cid:04d}_{safe_label(nick)}" if nick else f"card_{cid:04d}_deck{did}_idx{index:02d}"

            face_url = info.get("FaceURL")
            if face_url:
                face_url = clean_extracted_url(face_url)
                sheet = open_image_from_cache(face_url, sheet_cache)
                if sheet is not None:
                    try:
                        img = crop_card(sheet, index, nw, nh)
                        fname = f"{base}_front.png"
                        out, fname = unique_path(cards_dir, fname)
                        img.save(out, "PNG")
                        log(t("front_ok").format(fname), "ok")
                        cropped_front += 1
                        append_to_csv(csv_path, f"Card_{cid}_front", face_url, fname)
                    except Exception as e:
                        log(t("front_fail").format(cid, e), "err")
                        failed += 1
                else:
                    log(t("front_warn").format(cid, did), "warn")

            back_url = info.get("BackURL")
            if back_url:
                back_url = clean_extracted_url(back_url)
                sheet_b = open_image_from_cache(back_url, sheet_cache)
                if sheet_b is not None:
                    try:
                        img = crop_card(sheet_b, index, nw, nh) if unique_back else sheet_b
                        fname = f"{base}_back.png"
                        out, fname = unique_path(cards_dir, fname)
                        img.save(out, "PNG")
                        log(t("back_ok").format(fname), "ok")
                        cropped_back += 1
                        append_to_csv(csv_path, f"Card_{cid}_back", back_url, fname)
                    except Exception as e:
                        log(t("back_fail").format(cid, e), "err")
                        failed += 1
                else:
                    log(t("back_warn").format(cid, did), "warn")

            self._tick_progress(1, phase_cards)

        if self.cancel_flag.is_set():
            self._set_status(t("status_cancelled"))
            return

        try:
            if os.path.isdir(sheets_dir) and os.listdir(sheets_dir):
                zip_path = os.path.join(download_path, "sheets.zip")
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(sheets_dir):
                        for name in files:
                            full = os.path.join(root, name)
                            arc = os.path.relpath(full, sheets_dir)
                            zf.write(full, arc)
                shutil.rmtree(sheets_dir, ignore_errors=True)
                log(t("sheets_zipped").format(zip_path), "info")
            elif os.path.isdir(sheets_dir):
                shutil.rmtree(sheets_dir, ignore_errors=True)
        except Exception as e:
            log(f"sheets zip: {e}", "warn")

        phase_others = t("status_downloading_others")
        self._set_status(phase_others)
        log(t("others_header").format(MAX_WORKERS), "header")
        seen_u, other_tasks = set(), []
        for kind, url, label in others:
            url = clean_extracted_url(url)
            if url in seen_u:
                continue
            seen_u.add(url)
            other_tasks.append((kind, f"{kind}_{safe_label(label)}", url, kind_to_dir.get(kind, image_dir)))

        def _dl_other(args):
            if self.cancel_flag.is_set():
                return None
            _, field, url, dest = args
            return field, url, download_file(url, dest, field, log, t)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for fut in as_completed([ex.submit(_dl_other, x) for x in other_tasks]):
                if self.cancel_flag.is_set():
                    break
                result = fut.result()
                if result is None:
                    continue
                field, url, (status, _, filename, _) = result
                append_to_csv(csv_path, field, url, filename or "")
                if status == "success":
                    successful += 1
                elif status == "omitted":
                    skipped += 1
                else:
                    failed += 1
                self._tick_progress(1, phase_others)

        if self.cancel_flag.is_set():
            self._set_status(t("status_cancelled"))
            return

        stl_ok = stl_fail = 0
        self._set_status(t("status_converting_stl"))
        stl_ok, stl_fail = convert_models_folder(model_dir, log_fn=log, t_fn=t)

        try:
            remove_empty_dirs(download_path)
        except Exception:
            pass

        log("")
        log("=" * 50, "header")
        log(f"           {t('summary')}", "header")
        log("=" * 50, "header")
        if source_label:
            log(f"{t('json_input')}      {source_label}")
        log(f"{t('save_name')}             {safe_name}")
        log(f"{t('decks')}    {len(decks)}")
        log(f"{t('front_cards')}       {cropped_front}")
        log(f"{t('back_cards')}       {cropped_back}")
        log(f"{t('success')}   {successful}")
        log(f"{t('failed')}             {failed}")
        log(f"{t('skipped')}             {skipped}")
        log(f"{t('stl_converted')}       {stl_ok}")
        log(f"{t('stl_failed')}         {stl_fail}")
        log(f"{t('csv_path')}          {csv_path}")
        log_path = os.path.join(download_path, f"{safe_name}_log.txt")
        log(f"{t('log_file')}          {log_path}")
        log("")
        log(t("folder_structure"))
        log(t("sheets_zip_desc"))
        log(t("cards_desc"))
        log(t("token_desc"))
        log(t("model_stl_desc"))
        log(t("pdf_desc"))
        log(t("image_desc"))
        log("=" * 50, "header")

        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(self._log_lines) + "\n")
        except Exception as e:
            log(f"log file: {e}", "warn")

        self._set_progress_pct(100, t("status_done"))
        msg = t("done_msg").format(
            cropped_front, cropped_back, successful, failed, skipped, download_path
        )
        self.after(0, lambda: messagebox.showinfo(t("done_title"), msg))


def main():
    try:
        languages = load_languages()
    except Exception as e:
        print("ERROR:", e)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Error", str(e))
            root.destroy()
        except Exception:
            pass
        input("Enter para salir...")
        sys.exit(1)

    try:
        app = App(languages)
        app.mainloop()
    except Exception as e:
        print("ERROR al abrir la ventana:")
        traceback.print_exc()
        input("Enter para salir...")
        sys.exit(1)


if __name__ == "__main__":
    main()
