"""Motor de consulta: planifica, busca, investiga en vivo (opcional) y responde con citas."""
import json
import os
import re
import sqlite3
import sys
from typing import Callable

from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console

from openeye.collect import ROOT, connect, live_jobs, load_config, save

console = Console(stderr=True)
PRIVATE = {"file"}  # fuentes que solo se consultan con --source file

PLAN = (
    "Convierte la petición del usuario en un plan de búsqueda. Responde SOLO con JSON: "
    '{"entity": "nombre exacto de la persona, empresa o tema principal, sin verbos ni '
    'palabras como investiga/busca", "terms": ["hasta 6 palabras clave en español e inglés"], '
    '"domain": "dominio web si la petición menciona uno, si no null"}'
)

SYSTEM = (
    "Eres un analista OSINT. Responde SIEMPRE en español y basándote ÚNICAMENTE en los "
    "documentos proporcionados; no uses conocimiento externo. "
    "Cita cada dato solo con su número entre corchetes, por ejemplo [2]; NO escribas URLs, "
    "el programa las añade. Indica de qué tipo de fuente viene cada dato (noticias, sanciones, "
    "wikipedia, whois...). Compara los documentos entre sí: separa coincidencias respaldadas "
    "por varias fuentes de datos que aparecen en una sola, y señala discrepancias o falta de "
    "evidencia. No cuentes el mismo contenido republicado como corroboración independiente ni "
    "infieras una relación solo porque dos nombres aparecen juntos. Cita cada lado de una "
    "discrepancia. Sé breve y no repitas datos. "
    "Si la petición es sobre una persona o entidad concreta, usa solo datos que claramente "
    "sean de ella; no listes a otras personas por apellido parecido, y advierte si un "
    "dato podría ser un homónimo. "
    "Si los documentos no contienen información suficiente, dilo claramente."
)


def make_llm() -> Callable[[str, str, int], str]:
    """Crea una función llm(system, user, max_tokens) con fallback de modelo."""
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    env = cfg.get("api_key_env", "OPENROUTER_API_KEY")
    if not os.getenv(env):
        raise SystemExit(f"Falta {env} en .env")
    client = OpenAI(base_url=cfg.get("base_url", "https://openrouter.ai/api/v1"),
                    api_key=os.getenv(env))
    models = [m for m in (cfg.get("model"), cfg.get("fallback_model")) if m]

    def call(system: str, user: str, max_tokens: int) -> str:
        for m in models:
            try:
                r = client.chat.completions.create(
                    model=m, temperature=0.2, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}])
                return r.choices[0].message.content or ""
            except Exception as e:
                print(f"⚠ Falló {m}: {e}", file=sys.stderr)
        raise SystemExit("Ningún modelo respondió.")

    return call


def plan(llm: Callable, question: str) -> dict:
    """Extrae entidad, términos y dominio de la petición; si falla, usa la petición tal cual."""
    try:
        data = json.loads(re.search(r"\{.*\}", llm(PLAN, question, 250), re.S).group(0))
        domain = str(data.get("domain") or "").strip().lower()
        return {
            "entity": str(data.get("entity") or question).strip(),
            "terms": [str(t) for t in (data.get("terms") or [])][:6],
            "domain": domain if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain) else None,
        }
    except Exception:
        return {"entity": question, "terms": [], "domain": None}


def _match(terms: list[str], op: str) -> str:
    """Arma una consulta FTS5 con términos entre comillas (evita errores de sintaxis)."""
    return f" {op} ".join('"%s"' % t.replace('"', "") for t in terms if t.strip())


def _fts(conn: sqlite3.Connection, match: str, limit: int, source: str) -> list[tuple]:
    """Top-N de una fuente vía FTS5, ordenado por relevancia."""
    if not match:
        return []
    try:
        return conn.execute(
            "SELECT d.source, d.url, d.title, d.content FROM docs_fts "
            "JOIN docs d ON d.id = docs_fts.rowid "
            "WHERE docs_fts MATCH ? AND d.source = ? ORDER BY rank LIMIT ?",
            (match, source, limit)).fetchall()
    except sqlite3.OperationalError:
        return []  # consulta inválida para FTS5


def _like(conn: sqlite3.Connection, words: list[str], limit: int, source: str) -> list[tuple]:
    """Último recurso: búsqueda LIKE por cualquiera de las palabras."""
    if not words:
        return []
    where = " OR ".join("title LIKE ? OR content LIKE ?" for _ in words)
    params = [f"%{w}%" for w in words for _ in range(2)]
    return conn.execute(
        f"SELECT source, url, title, content FROM docs WHERE ({where}) AND source = ? LIMIT ?",
        (*params, source, limit)).fetchall()


def search(conn: sqlite3.Connection, entity: str, terms: list[str], limit: int,
           source: str | None = None) -> list[tuple]:
    """Por fuente: frase exacta → todas las palabras → cualquiera de las palabras."""
    if source:
        sources, per = [source], limit
    else:
        sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM docs") if r[0] not in PRIVATE]
        per = max(3, limit // 2)
    ent_words = list(dict.fromkeys(w for w in re.findall(r"\w+", entity) if len(w) > 2))
    all_words = list(dict.fromkeys(
        w for t in [entity, *terms] for w in re.findall(r"\w+", t) if len(w) > 2))
    out = []
    for s in sources:
        out += (_fts(conn, _match([entity], "AND"), per, s)
                or _fts(conn, _match(ent_words, "AND"), per, s)
                or _fts(conn, _match(all_words, "OR"), per, s)
                or _like(conn, all_words, per, s))
    return out


def build_context(docs: list[tuple]) -> str:
    """Formatea los documentos como contexto numerado para el LLM."""
    return "\n\n".join(
        f"[{i}] Fuente: {src}\nTítulo: {title}\n{content[:2000]}"
        for i, (src, _, title, content) in enumerate(docs, 1))


def run(question: str, source: str | None = None, top: int | None = None, live: bool = False) -> dict:
    """Planifica, (opcional) consulta fuentes en vivo, busca y responde. Devuelve un dict."""
    top = top or load_config().get("top", 8)
    llm = make_llm()
    with console.status("Planificando búsqueda…"):
        p = plan(llm, question)
    conn = connect()
    if live:
        for name, job in live_jobs(p["entity"], p["domain"]):
            with console.status(f"Consultando {name}…"):
                try:
                    new = save(conn, job())
                except Exception as e:  # una fuente caída no detiene el resto
                    console.print(f"[yellow]⚠ {name}: {e}[/]")
                    continue
            console.print(f"[green]✔[/] {name}: {new} documentos nuevos")
    docs = search(conn, p["entity"], p["terms"], top, source)
    conn.close()
    res = {"question": question, "plan": p, "answer": "", "sources": []}
    if not docs:
        res["answer"] = ("No encontré documentos relevantes. Prueba `openeye investigate` "
                         "para buscar en vivo, o `openeye collect` para actualizar la base.")
        return res
    with console.status("Redactando respuesta…"):
        res["answer"] = llm(SYSTEM, f"Documentos:\n{build_context(docs)}\n\nPetición: {question}", 3000)
    cited = sorted({int(n) for g in re.findall(r"\[([\d,\s]+)\]", res["answer"])
                    for n in g.split(",") if n.strip().isdigit() and 0 < int(n) <= len(docs)})
    res["sources"] = [{"n": n, "source": docs[n - 1][0], "title": docs[n - 1][2],
                       "url": docs[n - 1][1]} for n in cited]
    return res