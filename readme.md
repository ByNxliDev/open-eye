# Open Eye

CLI OSINT en español que recopila documentos de fuentes configuradas y permite
consultarlos con respuestas respaldadas por citas. Al responder, compara los
documentos disponibles, separa coincidencias corroboradas de datos aislados y
advierte sobre discrepancias o falta de evidencia. No trata contenido repetido
como corroboración independiente ni deduce relaciones solo por nombres compartidos.

## Preparación

Requiere Python 3.10 o posterior. Instala el proyecto desde la raíz:

```powershell
python -m pip install -e .
```

Configura la clave del proveedor de modelos indicada por `api_key_env` en
`config.yaml` dentro de un archivo `.env` en la raíz del proyecto. El valor
predeterminado es `OPENROUTER_API_KEY`.

## Uso

```powershell
openeye collect
openeye status
openeye ask "¿Qué información coincide sobre Open-source intelligence?"
openeye investigate "Open-source intelligence"
```

- `collect` descarga las fuentes de `config.yaml` y las guarda en `data/openeye.db`.
- `ask` busca solo en los documentos ya guardados.
- `investigate` consulta fuentes en vivo configuradas para la entidad y luego
  busca entre los documentos disponibles.
- Ambos modos de consulta comparan las evidencias recuperadas y citan las
  fuentes usadas. `--source` limita la consulta a una fuente; `--top` controla
  cuántos documentos considera.
- Los archivos de `files` se conservan como fuente privada: solo se consultan
  al indicar `--source file`.

La correlación es una comparación de evidencias documentales mediante el modelo
configurado; no es una unión automática de columnas ni demuestra por sí sola que
dos menciones correspondan a la misma persona o entidad.
