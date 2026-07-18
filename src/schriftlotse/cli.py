from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from schriftlotse.app import launch
from schriftlotse.benchmarks import (
    evaluate_search,
    run_cloud_manifest,
    run_public_gold,
    run_public_search_benchmark,
)
from schriftlotse.cloud import OpenRouterReviewer
from schriftlotse.config import AppPaths, Settings
from schriftlotse.database import Database
from schriftlotse.domain import (
    CloudPolicy,
    DocumentRequest,
    QualityProfile,
    ScriptHint,
    SearchMode,
    SearchQuery,
)
from schriftlotse.model_registry import MODELS, ModelManager
from schriftlotse.pipeline import ProcessingPipeline
from schriftlotse.search import ArchiveSearch

app = typer.Typer(help="SchriftLotse – historische Dokumente lokal entziffern und durchsuchen.")
models_app = typer.Typer(help="Freie lokale Modelle verwalten.")
benchmark_app = typer.Typer(help="OCR- und Suchqualität gegen bekannte Soll-Ergebnisse messen.")
app.add_typer(models_app, name="models")
app.add_typer(benchmark_app, name="benchmark")
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
def serve(
    port: Annotated[int, typer.Option("--port", min=1024, max=65535)] = 7860,
) -> None:
    """Lokalen Server ohne zusätzliches Browserfenster starten."""
    launch(open_browser=False, port=port)


@app.command()
def batch(
    source: Annotated[Path, typer.Argument(exists=True, readable=True)],
    year: Annotated[int | None, typer.Option("--year", "-y")] = None,
    script: Annotated[ScriptHint, typer.Option("--script", "-s")] = ScriptHint.AUTO,
    profile: Annotated[QualityProfile, typer.Option("--profile", "-p")] = QualityProfile.BEST_LOCAL,
    cloud: Annotated[bool, typer.Option("--cloud/--no-cloud")] = False,
    budget: Annotated[float, typer.Option("--budget")] = 1.0,
    advanced: Annotated[bool, typer.Option("--advanced/--basic")] = True,
    resume_job: Annotated[str | None, typer.Option("--resume-job")] = None,
) -> None:
    """Datei oder Ordner als Stapel verarbeiten."""
    pipeline = ProcessingPipeline()
    request = DocumentRequest(
        sources=[source],
        year=year,
        script_hint=script,
        cloud_policy=CloudPolicy.LOCAL_ONLY,
        cloud_budget_usd=budget,
        advanced_models=advanced,
        quality_profile=profile if advanced else QualityProfile.FAST,
    )
    if cloud:
        console.print(
            "[yellow]Cloud-Prüfung läuft aus Datenschutzgründen nur in der Oberfläche "
            "nach Auswahl einer einzelnen Fundstelle.[/]"
        )
    highest_progress = 0.0

    def report_progress(message: str, value: float) -> None:
        nonlocal highest_progress
        # The total can grow when a scan is recognized as a double page. Keep
        # the displayed percentage monotonic even though the denominator changed.
        highest_progress = max(highest_progress, value)
        console.print(f"[{highest_progress:>6.1%}] {message}")

    job_id, results, exports = pipeline.run(
        request,
        progress=report_progress,
        job_id=resume_job,
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


@app.command("import-pagexml")
def import_pagexml(
    document_id: Annotated[str, typer.Argument()],
    files: Annotated[list[Path], typer.Argument(exists=True, readable=True)],
) -> None:
    """In eScriptorium korrigierte PAGE-XML-Dateien zurückspielen."""
    database = Database(AppPaths.default().database)
    changed = database.import_pagexml_corrections(document_id, files)
    console.print(f"[green]{changed} korrigierte Zeilen übernommen.[/]")


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
def model_install(
    key: Annotated[str, typer.Argument()],
    accept_license: Annotated[bool, typer.Option("--accept-license")] = False,
) -> None:
    if key not in MODELS:
        raise typer.BadParameter(f"Unbekanntes Modell. Verfügbar: {', '.join(MODELS)}")
    path = ModelManager(AppPaths.default()).install(key, accept_license=accept_license)
    console.print(f"[green]{MODELS[key].name} installiert:[/] {path}")


@models_app.command("core-ready", hidden=True)
def core_models_ready() -> None:
    """Mit Exit-Code anzeigen, ob die empfohlenen Kernmodelle bereitstehen."""
    manager = ModelManager(AppPaths.default())
    missing = [
        key
        for key in (
            "trocr-kurrent-19",
            "trocr-kurrent-early",
            "ub-german-handwriting",
            "party-v4",
            "qwen-embed",
        )
        if not manager.is_installed(key)
    ]
    if missing:
        raise typer.Exit(1)


@models_app.command("install-core")
def install_core_models() -> None:
    """Lizenzklare Kurrent- und Ganzseitenmodelle installieren."""
    manager = ModelManager(AppPaths.default())
    for key in (
        "trocr-kurrent-19",
        "trocr-kurrent-early",
        "ub-german-handwriting",
        "party-v4",
        "qwen-embed",
    ):
        if manager.is_installed(key):
            console.print(f"[dim]{MODELS[key].name} ist bereits installiert.[/]")
            continue
        console.print(f"[cyan]{MODELS[key].name} wird geladen und geprüft …[/]")
        path = manager.install(key)
        console.print(f"[green]{MODELS[key].name} installiert:[/] {path}")


@models_app.command("best-ready", hidden=True)
def best_model_ready() -> None:
    """Mit Exit-Code anzeigen, ob der lokale Standard-Zweitleser bereitsteht."""
    if not ModelManager(AppPaths.default()).is_installed("churro-mlx-8bit"):
        raise typer.Exit(1)


@models_app.command("install-best")
def install_best_model(
    accept_research_license: Annotated[bool, typer.Option("--accept-research-license")] = False,
) -> None:
    """CHURRO als lokalen Standard-Zweitleser für die Forschungsnutzung installieren."""
    if not accept_research_license:
        raise typer.BadParameter("CHURRO benötigt die Bestätigung der Qwen Research License")
    manager = ModelManager(AppPaths.default())
    key = "churro-mlx-8bit"
    if manager.is_installed(key):
        console.print(f"[dim]{MODELS[key].name} ist bereits installiert.[/]")
        return
    console.print("[cyan]CHURRO wird geladen und für Apple Silicon in MLX 8-Bit umgewandelt …[/]")
    path = manager.install(key, accept_license=True)
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


@benchmark_app.command("gold")
def benchmark_gold(
    dataset: Annotated[str, typer.Argument(help="kurrent-19 oder kurrent-1665")],
    sample_size: Annotated[int, typer.Option("--sample", min=8, max=1000)] = 96,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Öffentlichen, unabhängigen PAGE-XML-Goldstandard herunterladen und messen."""
    result = run_public_gold(dataset, sample_size)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
        console.print(f"[green]Benchmarkbericht gespeichert:[/] {output}")
    console.print_json(encoded)


@benchmark_app.command("search")
def benchmark_search(
    qrels: Annotated[Path, typer.Argument(exists=True, readable=True)],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 10,
) -> None:
    """Suchindex mit privaten Query-Relevance-Urteilen (JSON) bewerten."""
    paths = AppPaths.default()
    result = evaluate_search(
        ArchiveSearch(Database(paths.database), ModelManager(paths)), qrels, limit
    )
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2))


@benchmark_app.command("search-public")
def benchmark_search_public(
    dataset: Annotated[str, typer.Argument(help="kurrent-19 oder kurrent-1665")],
    queries: Annotated[int, typer.Option("--queries", min=10, max=500)] = 40,
) -> None:
    """Suche mit echten Goldtextzeilen und reproduzierbaren OCR-Tippfehlern messen."""
    result = run_public_search_benchmark(dataset, queries)
    console.print_json(json.dumps(result, ensure_ascii=False, indent=2))


@benchmark_app.command("cloud")
def benchmark_cloud(
    manifest: Annotated[Path, typer.Argument(exists=True, readable=True)],
    budget: Annotated[float, typer.Option("--budget", min=0.1, max=20.0)] = 2.0,
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Bis zu acht private Goldausschnitte mit vier kuratierten Cloudmodellen messen."""
    result = run_cloud_manifest(manifest, budget)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    console.print_json(encoded)
