"""Interfaz de línea de comandos de open-eye."""
import csv
import json
import random
import unicodedata
from pathlib import Path

import typer
from openpyxl import load_workbook
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from openeye.ask import run
from openeye.collect import connect, read_text, run_all

app = typer.Typer(help="open-eye: OSINT minimalista.", add_completion=False)
console = Console()

SOURCE = typer.Option(None, "--source", "-s", help="web, rss, wiki, whois, sanctions, crtsh o file")
TOP = typer.Option(None, "--top", "-t", help="Documentos a consultar (por defecto, config.yaml)")
JSON = typer.Option(False, "--json", help="Salida en JSON")
OUT = typer.Option(None, "--out", "-o", help="Guardar el informe en un archivo .md")


def _short(s: str, n: int) -> str:
    """Recorta un texto largo con puntos suspensivos."""
    return s if len(s) <= n else s[: n - 1] + "…"


def to_markdown(res: dict) -> str:
    """Convierte el resultado en un informe Markdown."""
    lines = [f"# {res['question']}", "", res["answer"], "", "## Fuentes"]
    lines += [f"{s['n']}. ({s['source']}) [{s['title']}]({s['url']})" for s in res["sources"]]
    return "\n".join(lines) + "\n"


def show(res: dict, as_json: bool = False, out: Path | None = None) -> None:
    """Muestra el resultado (panel + tabla de fuentes citadas), JSON o lo guarda en archivo."""
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(to_markdown(res), encoding="utf-8")
    if as_json:
        typer.echo(json.dumps(res, ensure_ascii=False, indent=2))
        return
    console.print(Panel(Markdown(res["answer"]), title=_short(res["plan"]["entity"], 60),
                        border_style="cyan"))
    if res["sources"]:
        t = Table("#", "Fuente", "Título", "Enlace", header_style="bold")
        for s in res["sources"]:
            t.add_row(str(s["n"]), s["source"], _short(s["title"], 35), _short(s["url"], 60))
        console.print(t)
    else:
        console.print("[dim]Sin fuentes citadas.[/]")
    if out:
        console.print(f"[green]Informe guardado en {out}[/]")


@app.command()
def collect() -> None:
    """Recolecta todas las fuentes definidas en config.yaml."""
    run_all()


@app.command()
def ask(question: str = typer.Argument(..., help="Pregunta en lenguaje natural"),
        source: str = SOURCE, top: int = TOP, json_out: bool = JSON, out: Path = OUT) -> None:
    """Responde con lo ya guardado en la base."""
    show(run(question, source, top), json_out, out)


@app.command()
def investigate(entity: str = typer.Argument(..., help="Persona, empresa, tema o dominio"),
                source: str = SOURCE, top: int = TOP, json_out: bool = JSON, out: Path = OUT) -> None:
    """Busca en vivo (noticias, Wikipedia, whois, certificados) y responde."""
    show(run(entity, source, top, live=True), json_out, out)


@app.command()
def status() -> None:
    """Muestra cuántos documentos hay por fuente."""
    conn = connect()
    rows = conn.execute(
        "SELECT source, COUNT(*), MAX(created_at) FROM docs GROUP BY source ORDER BY 2 DESC").fetchall()
    conn.close()
    t = Table("Fuente", "Documentos", "Última carga", header_style="bold")
    for src, n, last in rows:
        t.add_row(src + (" (privada)" if src == "file" else ""), str(n), last or "")
    console.print(t if rows else "[dim]La base está vacía. Ejecuta `openeye collect`.[/]")


@app.command()
def chat(source: str = SOURCE) -> None:
    """Modo interactivo. Prefija con ! para investigar en vivo. 'salir' para terminar."""
    console.print("[bold]open-eye chat[/] — cada pregunta es independiente (sin memoria). "
                  "'!tema' investiga en vivo; 'salir' termina.")
    while True:
        try:
            q = console.input("[bold cyan]> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in {"salir", "exit", "q"}:
            break
        if not q:
            continue
        try:
            show(run(q.lstrip("!").strip(), source, None, live=q.startswith("!")))
        except SystemExit as e:
            console.print(f"[red]{e}[/]")


# ---------- export: filtra un Excel/CSV en local, sin enviar nada a la IA ----------

def _norm(s) -> str:
    """Minúsculas y sin tildes, para comparar texto."""
    return unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower().strip()


def _load_rows(path: Path) -> list[tuple]:
    """Lee las filas de un Excel (aunque se llame .csv) o de un CSV normal."""
    with path.open("rb") as f:
        is_zip = f.read(4) == b"PK\x03\x04"
    if is_zip:
        wb = load_workbook(path, read_only=True, data_only=True)
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
        wb.close()
        return rows
    return [tuple(r) for r in csv.reader(read_text(path).splitlines())]


@app.command()
def export(
    file: Path = typer.Argument(..., help="Excel o CSV"),
    value: str = typer.Argument(..., help="Texto a buscar"),
    column: str = typer.Option("pais origen", "--column", "-c", help="Columna donde buscar"),
    n: int = typer.Option(20, "--n", "-n", help="Filas aleatorias a exportar"),
    exact: bool = typer.Option(False, "--exact", "-e", help="Coincidencia exacta (ej. códigos como PA)"),
    out: Path = typer.Option(Path("data/export.csv"), "--out", "-o"),
) -> None:
    """Filtra filas por columna, toma una muestra aleatoria y la guarda en CSV (todo local)."""
    rows = _load_rows(file)
    if not rows:
        raise SystemExit("El archivo está vacío.")
    header = [str(h or "").strip() for h in rows[0]]
    names = [_norm(h) for h in header]
    if _norm(column) not in names:
        raise SystemExit(f"No existe la columna '{column}'. Columnas: {', '.join(header)}")
    i = names.index(_norm(column))
    cell = lambda r: _norm(r[i]) if i < len(r) else ""
    match = (lambda c: c == _norm(value)) if exact else (lambda c: _norm(value) in c)
    data = [r for r in rows[1:] if cell(r) and match(cell(r))]
    if not data:
        vals = sorted({str(r[i]) for r in rows[1:] if i < len(r) and r[i]})[:15]
        raise SystemExit(f"0 filas con '{value}' en '{column}'. Valores de ejemplo: {vals}")
    sample = random.sample(data, min(n, len(data)))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(sample)
    typer.echo(f"{len(data)} filas coinciden; {len(sample)} exportadas a {out}")


if __name__ == "__main__":
    app()