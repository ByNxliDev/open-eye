"""Fuentes de datos: recolección masiva (collect) y consultas en vivo (investigate)."""
import csv
import hashlib
import io
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import feedparser
import httpx
import trafilatura
import whois
import yaml
from openpyxl import load_workbook
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "openeye.db"
CONFIG_PATH = ROOT / "config.yaml"
HEADERS = {"User-Agent": "open-eye/0.2 (research tool)"}
FILE_TYPES = {".txt", ".md", ".csv", ".html", ".pdf", ".xlsx"}
CHUNK = 3000

# Un documento = (source, url, title, content)
Doc = tuple[str, str, str, str]

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(
    id INTEGER PRIMARY KEY,
    source TEXT,
    url TEXT UNIQUE,
    title TEXT,
    content TEXT,
    created_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, content, content='docs', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
END;
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, hash TEXT);
"""


def load_config() -> dict:
    """Lee config.yaml."""
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def connect() -> sqlite3.Connection:
    """Abre data/openeye.db y crea las tablas si no existen."""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def _get(url: str, **kw) -> httpx.Response:
    """GET con UA, timeout y redirecciones."""
    r = httpx.get(url, headers=HEADERS, timeout=kw.pop("timeout", 30), follow_redirects=True, **kw)
    r.raise_for_status()
    return r


# ---------- Fuentes de recolección masiva ----------

def fetch_web(url: str) -> list[Doc]:
    """Descarga una página y extrae su texto principal."""
    html = _get(url).text
    text = trafilatura.extract(html)
    if not text:
        return []
    meta = trafilatura.extract_metadata(html)
    title = meta.title if meta and meta.title else url
    return [("web", url, title, text)]


def fetch_rss(feed_url: str) -> list[Doc]:
    """Lee un feed y devuelve una entrada por documento."""
    feed = feedparser.parse(_get(feed_url).content)
    docs = []
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        title = e.get("title", link)
        summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))
        docs.append(("rss", link, title, f"{title}\n{summary}".strip()))
    return docs


def fetch_whois(domain: str) -> list[Doc]:
    """Consulta WHOIS de un dominio."""
    w = whois.whois(domain)
    content = "\n".join(f"{k}: {v}" for k, v in w.items() if v)
    if not content:
        return []
    return [("whois", f"whois://{domain}", f"WHOIS {domain}", content)]


def fetch_sanctions(url: str) -> list[Doc]:
    """Descarga un CSV de sanciones (ej. OFAC SDN): una fila por documento."""
    docs = []
    for row in csv.reader(io.StringIO(_get(url).text)):
        if len(row) < 2:
            continue
        fields = [c for c in row[1:] if c and c != "-0-"]
        docs.append(("sanctions", f"{url}#{row[0]}", row[1], " | ".join(fields)))
    return docs


# ---------- Archivos locales ----------

def read_text(p: Path) -> str:
    """Lee texto detectando BOM UTF-16/UTF-8 y con cp1252 como alternativa."""
    raw = p.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _read_xlsx(p: Path) -> str:
    """Convierte las hojas de un Excel a texto CSV (una fila por línea)."""
    wb = load_workbook(p, read_only=True, data_only=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).replace("\n", " ") for c in row]
            if any(cells):
                w.writerow(cells)
    wb.close()
    return buf.getvalue()


def _is_zip(p: Path) -> bool:
    """True si el archivo es un zip (.xlsx), aunque se llame .csv."""
    with p.open("rb") as f:
        return f.read(4) == b"PK\x03\x04"


def _read_file(p: Path) -> str:
    """Extrae el texto de un archivo según su contenido real, no solo su extensión."""
    if _is_zip(p):
        return _read_xlsx(p)
    if p.suffix.lower() == ".pdf":
        return "\n".join(pg.extract_text() or "" for pg in PdfReader(p).pages)
    text = read_text(p)
    return (trafilatura.extract(text) or "") if p.suffix.lower() == ".html" else text


def _chunks(text: str, tabular: bool) -> list[str]:
    """Parte el texto en trozos; en tablas agrupa filas y repite el encabezado."""
    if not tabular:
        return [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    header, *rows = text.splitlines() or [""]
    out, cur = [], header
    for row in rows:
        if len(cur) + len(row) > CHUNK and cur != header:
            out.append(cur)
            cur = header
        cur += "\n" + row
    out.append(cur)
    return out


def fetch_files(path: str) -> list[Doc]:
    """Lee archivos nuevos o modificados (por hash) y los parte en trozos."""
    root = Path(path).expanduser()
    root = root if root.is_absolute() else ROOT / root
    if not root.exists():
        raise FileNotFoundError(f"no existe {root}")
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    conn, docs = connect(), []
    for p in files:
        if not p.is_file() or p.suffix.lower() not in FILE_TYPES:
            continue
        key = p.resolve().as_uri()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        row = conn.execute("SELECT hash FROM files WHERE path = ?", (key,)).fetchone()
        if row and row[0] == digest:
            continue  # sin cambios
        try:
            text = _read_file(p)
        except Exception as e:  # un archivo roto no detiene el resto
            print(f"⚠ [files] {p.name}: {e}")
            continue
        prefix = key + "#"  # si cambió, se borran sus trozos viejos
        conn.execute("DELETE FROM docs WHERE substr(url, 1, ?) = ?", (len(prefix), prefix))
        conn.execute("INSERT OR REPLACE INTO files(path, hash) VALUES (?, ?)", (key, digest))
        conn.commit()
        tabular = p.suffix.lower() in {".csv", ".xlsx"} or _is_zip(p)
        for n, chunk in enumerate(_chunks(text, tabular), 1):
            if chunk.strip():
                docs.append(("file", f"{prefix}{n}", p.name, chunk.strip()))
    conn.close()
    return docs


# ---------- Fuentes en vivo (investigate) ----------

def fetch_news(query: str) -> list[Doc]:
    """Noticias recientes sobre una frase exacta (Google News RSS, español e inglés)."""
    q = urllib.parse.quote(f'"{query}"')
    docs = []
    for hl, ceid in (("es-419", "PA:es-419"), ("en-US", "US:en")):
        docs += fetch_rss(f"https://news.google.com/rss/search?q={q}&hl={hl}&ceid={ceid}")
    return docs


def fetch_wikipedia(query: str, lang: str = "es") -> list[Doc]:
    """Introducción de los 2 artículos de Wikipedia más relevantes."""
    r = _get(f"https://{lang}.wikipedia.org/w/api.php", params={
        "action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": 2,
        "prop": "extracts", "exintro": 1, "explaintext": 1, "format": "json",
    })
    pages = ((r.json().get("query") or {}).get("pages") or {}).values()
    return [
        ("wiki", f"https://{lang}.wikipedia.org/?curid={pg['pageid']}", pg["title"], pg["extract"])
        for pg in pages if pg.get("extract")
    ]


def fetch_crtsh(domain: str) -> list[Doc]:
    """Subdominios vistos en certificados públicos (crt.sh)."""
    rows = _get("https://crt.sh/", params={"q": f"%.{domain}", "output": "json"}, timeout=60).json()
    names = sorted({n.strip() for row in rows for n in row["name_value"].splitlines()})
    if not names:
        return []
    return [("crtsh", f"crtsh://{domain}", f"Certificados de {domain}",
             "Subdominios:\n" + "\n".join(names[:300]))]


def live_jobs(entity: str, domain: str | None) -> list[tuple[str, Callable[[], list[Doc]]]]:
    """Fuentes en vivo para una entidad. Para añadir una, agrega una línea aquí."""
    jobs = [
        ("noticias", lambda: fetch_news(entity)),
        ("wikipedia-es", lambda: fetch_wikipedia(entity, "es")),
        ("wikipedia-en", lambda: fetch_wikipedia(entity, "en")),
    ]
    if domain:
        jobs += [("whois", lambda: fetch_whois(domain)), ("crt.sh", lambda: fetch_crtsh(domain))]
    return jobs


# ---------- Guardado y ejecución ----------

def save(conn: sqlite3.Connection, docs: list[Doc]) -> int:
    """Inserta ignorando duplicados por url; devuelve cuántos son nuevos."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    count = lambda: conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    before = count()
    conn.executemany(
        "INSERT OR IGNORE INTO docs(source, url, title, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(*d, now) for d in docs],
    )
    conn.commit()
    return count() - before


def run_all() -> None:
    """Ejecuta todas las fuentes de config.yaml e imprime un resumen."""
    cfg = load_config()
    conn = connect()
    jobs = [
        ("web", fetch_web, cfg.get("urls") or []),
        ("rss", fetch_rss, cfg.get("rss") or []),
        ("whois", fetch_whois, cfg.get("domains") or []),
        ("sanctions", fetch_sanctions, cfg.get("sanctions_urls") or []),
        ("files", fetch_files, cfg.get("files") or []),
    ]
    total = 0
    for name, fn, items in jobs:
        new = 0
        for item in items:
            try:
                new += save(conn, fn(item))
            except Exception as e:  # una fuente caída no detiene el resto
                print(f"⚠ [{name}] {item}: {e}")
        print(f"✔ {name}: {new} documentos nuevos")
        total += new
    print(f"\nTotal: {total} documentos nuevos guardados en {DB_PATH}")
    conn.close()