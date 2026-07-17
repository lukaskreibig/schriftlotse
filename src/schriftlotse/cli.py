from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from schriftlotse.app import launch
from schriftlotse.cloud import OpenRouterReviewer
from schriftlotse.config import AppPaths, Settings
from schriftlotse.database import Database
from schriftlotse.domain import CloudPolicy, DocumentRequest, ScriptHint, SearchMode, SearchQuery
from schriftlotse.model_registry import MODELS, ModelManager
from schriftlotse.pipeline import ProcessingPipeline
from schriftlotse.search import ArchiveSearch

app = typer.Typer(help="SchriftLotse – historische Dokumente lokal entziffern und durchsuchen.")
models_app = typer.Typer(help="Freie lokale Modelle verwalten.")
app.add_typer(models_app, name="models")
console = Console()


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        launch()


@app.command()
def gui() -> None:
    """Deutsche lokale Oberfläche starten."""
    launch()


@app.command()
def batch(
    source: Annotated[Path, typer.Argument(exists=True, readable=True)],
    year: Annotated[int | None, typer.Option("--year", "-y")] = None,
    script: Annotated[ScriptHint, typer.Option("--script", "-s")] = ScriptHint.AUTO,
    cloud: Annotated[bool, typer.Option("--cloud/--no-cloud")] = False,
    budget: Annotated[float, typer.Option("--budget")] = 1.0,
    advanced: Annotated[bool, typer.Option("--advanced/--basic")] = True,
) -> None:
    """Datei oder Ordner als Stapel verarbeiten."""
    pipeline = ProcessingPipeline()
    request = DocumentRequest(
        sources=[source],
        year=year,
        script_hint=script,
        cloud_policy=CloudPolicy.ADAPTIVE if cloud else CloudPolicy.LOCAL_ONLY,
        cloud_budget_usd=budget,
        advanced_models=advanced,
    )
    job_id, results, exports = pipeline.run(
        request, progress=lambda message, value: console.print(f"[{value:>6.1%}] {message}")
    )
    console.print(f"[green]Auftrag {job_id[:8]} abgeschlossen:[/] {len(results)} Dokumente")
    for path in exports:
        console.print(path)


@app.command("search")
def search_command(
    text: Annotated[str, typer.Argument()],
    mode: Annotated[SearchMode, typer.Option("--mode", "-m")] = SearchMode.SMART,
    fuzziness: Annotated[float, typer.Option("--fuzziness", "-f")] = 0.72,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
) -> None:
    """Alle bereits verarbeiteten Dokumente durchsuchen."""
    paths = AppPaths.default()
    database = Database(paths.database)
    engine = ArchiveSearch(database, ModelManager(paths))
    hits = engine.search(SearchQuery(text=text, mode=mode, fuzziness=fuzziness, limit=limit))
    table = Table("Dokument", "Seite", "Text", "Trefferart", "Bewertung")
    for hit in hits:
        table.add_row(
            hit.document_title, str(hit.page_index + 1), hit.text, hit.reason, f"{hit.score:.0%}"
        )
    console.print(table)


@models_app.command("list")
def models_list() -> None:
    paths = AppPaths.default()
    manager = ModelManager(paths)
    table = Table("Schlüssel", "Modell", "Lizenz", "Status")
    for item in manager.status():
        table.add_row(
            item["key"],
            item["name"],
            item["license"],
            "installiert" if item["installed"] else "fehlt",
        )
    console.print(table)


@models_app.command("install")
def model_install(key: Annotated[str, typer.Argument()]) -> None:
    if key not in MODELS:
        raise typer.BadParameter(f"Unbekanntes Modell. Verfügbar: {', '.join(MODELS)}")
    path = ModelManager(AppPaths.default()).install(key)
    console.print(f"[green]{MODELS[key].name} installiert:[/] {path}")


@models_app.command("core-ready", hidden=True)
def core_models_ready() -> None:
    """Mit Exit-Code anzeigen, ob die empfohlenen Kernmodelle bereitstehen."""
    manager = ModelManager(AppPaths.default())
    missing = [key for key in ("trocr-kurrent-19",) if not manager.is_installed(key)]
    if missing:
        raise typer.Exit(1)


@models_app.command("install-core")
def install_core_models() -> None:
    """Kurrent-TrOCR samt vollständigem lokalem Prozessor installieren."""
    manager = ModelManager(AppPaths.default())
    for key in ("trocr-kurrent-19",):
        if manager.is_installed(key):
            console.print(f"[dim]{MODELS[key].name} ist bereits installiert.[/]")
            continue
        console.print(f"[cyan]{MODELS[key].name} wird geladen und geprüft …[/]")
        path = manager.install(key)
        console.print(f"[green]{MODELS[key].name} installiert:[/] {path}")


@app.command("set-openrouter-key")
def set_openrouter_key(key: Annotated[str, typer.Option(prompt=True, hide_input=True)]) -> None:
    """OpenRouter-Schlüssel im macOS-Schlüsselbund speichern."""
    OpenRouterReviewer.save_api_key(key)
    console.print("[green]Schlüssel sicher gespeichert.[/]")


@app.command()
def doctor() -> None:
    """Lokale Installation und Modellstatus prüfen."""
    paths = AppPaths.default()
    settings = Settings.load(paths)
    payload = {
        "python_ok": True,
        "tesseract": shutil.which(settings.tesseract_command),
        "database": str(paths.database),
        "output": str(paths.output),
        "models": ModelManager(paths).status(),
    }
    console.print_json(json.dumps(payload, ensure_ascii=False, default=str))
