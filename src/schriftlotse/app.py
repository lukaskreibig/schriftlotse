from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import gradio as gr
from PIL import ImageDraw

from schriftlotse.authority import GNDClient
from schriftlotse.cloud import OpenRouterReviewer
from schriftlotse.config import AppPaths, Settings
from schriftlotse.database import Database
from schriftlotse.domain import CloudPolicy, DocumentRequest, ScriptHint, SearchMode, SearchQuery
from schriftlotse.ingest import load_page
from schriftlotse.model_registry import MODELS, ModelManager
from schriftlotse.pipeline import ProcessingPipeline
from schriftlotse.search import ArchiveSearch

CSS = """
:root { --sl-dark: #173f4b; --sl-gold: #d29b3d; }
.gradio-container { max-width: 1380px !important; }
.sl-hero { background: linear-gradient(120deg, #173f4b, #285f6c); color: white;
  padding: 24px 28px; border-radius: 18px; margin-bottom: 14px; }
.sl-hero h1 { color: white; margin: 0 0 5px 0; }
.sl-hero p { margin: 0; opacity: .9; }
.sl-note { border-left: 4px solid #d29b3d; padding-left: 12px; }
"""


class UIController:
    def __init__(self) -> None:
        self.paths = AppPaths.default()
        self.paths.ensure()
        self.settings = Settings.load(self.paths)
        self.database = Database(self.paths.database)
        self.manager = ModelManager(self.paths)
        self.search_engine = ArchiveSearch(self.database, self.manager)
        self.gnd = GNDClient()

    @staticmethod
    def pick_folder() -> str:
        process = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Scan-Ordner auswählen")',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return process.stdout.strip() if process.returncode == 0 else ""

    def process(
        self,
        uploaded: list[str] | None,
        folder: str,
        year: float | None,
        script_label: str,
        advanced: bool,
        cloud: bool,
        budget: float,
        progress: gr.Progress = gr.Progress(),  # noqa: B008 - injected by Gradio
    ) -> tuple[str, list[str]]:
        sources = [Path(path) for path in (uploaded or [])]
        if folder.strip():
            sources.append(Path(folder.strip()))
        if not sources:
            raise gr.Error("Bitte mindestens eine Datei oder einen Ordner auswählen.")
        script_map = {
            "Automatisch": ScriptHint.AUTO,
            "Handschrift": ScriptHint.HANDWRITING,
            "Druck/Fraktur": ScriptHint.PRINT,
            "Schreibmaschine": ScriptHint.TYPEWRITER,
        }
        request = DocumentRequest(
            sources=sources,
            year=int(year) if year else None,
            script_hint=script_map[script_label],
            cloud_policy=CloudPolicy.ADAPTIVE if cloud else CloudPolicy.LOCAL_ONLY,
            cloud_budget_usd=float(budget),
            advanced_models=advanced,
        )
        pipeline = ProcessingPipeline(self.paths, self.settings, self.database)
        job_id, results, exports = pipeline.run(
            request,
            progress=lambda message, value: progress(value, desc=message),
        )
        summary = [f"## Auftrag `{job_id[:8]}` abgeschlossen", ""]
        for result in results:
            uncertain = sum(page.expected_cer > 0.10 for page in result.pages)
            summary.append(
                f"- **{result.document.title}**: {len(result.pages)} Seiten, "
                f"{uncertain} Seite(n) zur Prüfung, Ausgabe: `{result.output_dir}`"
            )
        return "\n".join(summary), [str(path) for path in exports if path.exists()]

    def search(
        self,
        text: str,
        mode_label: str,
        fuzziness: float,
        year_from: float | None,
        year_to: float | None,
    ) -> tuple[list[list[Any]], list[dict[str, Any]], str]:
        if not text.strip():
            return [], [], "Bitte einen Suchbegriff eingeben."
        mode_map = {
            "Intelligent": SearchMode.SMART,
            "Exakt": SearchMode.EXACT,
            "Namen": SearchMode.NAME,
            "Bedeutung": SearchMode.SEMANTIC,
        }
        hits = self.search_engine.search(
            SearchQuery(
                text=text,
                mode=mode_map[mode_label],
                fuzziness=float(fuzziness),
                year_from=int(year_from) if year_from else None,
                year_to=int(year_to) if year_to else None,
                limit=100,
            )
        )
        state = [hit.model_dump(mode="json") for hit in hits]
        rows = [
            [
                hit.document_title,
                hit.year or "",
                hit.page_index + 1,
                hit.text,
                hit.reason,
                f"{hit.score:.0%}",
                f"{hit.confidence:.0%}",
            ]
            for hit in hits
        ]
        note = (
            f"**{len(hits)} Treffer** – einen Treffer anklicken, "
            "um direkt zur Scan-Zeile zu springen."
        )
        return rows, state, note

    @staticmethod
    def select_hit(
        results: list[dict[str, Any]], event: gr.SelectData
    ) -> tuple[Any, str, str, str]:
        index = event.index[0] if isinstance(event.index, tuple) else int(event.index)
        if index < 0 or index >= len(results):
            return None, "", "", ""
        hit = results[index]
        path = Path(hit["source_path"])
        try:
            image = load_page(path, int(hit["page_index"]))
            draw = ImageDraw.Draw(image)
            bbox = tuple(hit["bbox"])
            draw.rectangle(bbox, outline="#e33b2f", width=max(3, image.width // 500))
        except (OSError, ValueError, IndexError):
            image = None
        details = (
            f"### {hit['document_title']} · Seite {int(hit['page_index']) + 1}\n\n"
            f"**Trefferart:** {hit['reason']}  \n"
            f"**Erkannter Text:** {hit['text']}"
        )
        return image, details, hit["text"], hit["line_id"]

    def save_correction(self, line_id: str, text: str) -> str:
        if not line_id:
            raise gr.Error("Zuerst einen Suchtreffer auswählen.")
        self.database.update_line(line_id, text.strip())
        return "Korrektur gespeichert; der Suchindex wurde aktualisiert."

    def lookup_gnd(self, name: str) -> str:
        if not name.strip():
            raise gr.Error("Bitte einen Namen eingeben oder einen Treffer auswählen.")
        try:
            results = self.gnd.search_person(name.strip())
        except Exception as error:
            raise gr.Error(f"GND-Abgleich nicht verfügbar: {error}") from error
        if not results:
            return "Keine passende Person in der GND gefunden."
        lines = ["### Optionale GND-Vorschläge", ""]
        for item in results:
            dates = "–".join([*(item["geburt"][:1]), *(item["tod"][:1])])
            occupation = ", ".join(item["beruf"][:3])
            lines.append(
                f"- [{item['name']}]({item['url']}) · GND {item['gnd_id']}"
                + (f" · {dates}" if dates else "")
                + (f" · {occupation}" if occupation else "")
            )
        lines.append("\n*Diese Vorschläge ändern die Transkription nicht automatisch.*")
        return "\n".join(lines)

    def model_status(self) -> list[list[Any]]:
        return [
            [
                row["name"],
                row["purpose"],
                row["license"],
                "Installiert" if row["installed"] else "Nicht installiert",
            ]
            for row in self.manager.status()
        ]

    def install_model(
        self,
        key: str,
        progress: gr.Progress = gr.Progress(),  # noqa: B008 - Gradio injection
    ) -> tuple[str, list[list[Any]]]:
        progress(0.05, desc=f"{MODELS[key].name} wird geladen")
        path = self.manager.install(key)
        progress(1.0, desc="Installation abgeschlossen")
        return f"{MODELS[key].name} wurde unter `{path}` installiert.", self.model_status()

    @staticmethod
    def save_key(api_key: str) -> str:
        if not api_key.strip():
            raise gr.Error("Bitte einen OpenRouter-Schlüssel eingeben.")
        OpenRouterReviewer.save_api_key(api_key)
        return "OpenRouter-Schlüssel wurde im macOS-Schlüsselbund gespeichert."


def build_app() -> gr.Blocks:
    controller = UIController()
    with gr.Blocks(title="SchriftLotse") as demo:
        gr.HTML(
            '<div class="sl-hero"><h1>SchriftLotse</h1>'
            "<p>Alte Schriften. Klar gelesen. Lokal, nachvollziehbar und durchsuchbar.</p></div>"
        )
        with gr.Tab("Entziffern"):
            with gr.Row():
                with gr.Column(scale=3):
                    uploaded = gr.File(
                        label="Scans oder PDFs hier ablegen",
                        file_count="multiple",
                        type="filepath",
                        file_types=["image", ".pdf", ".tif", ".tiff", ".heic", ".heif"],
                    )
                    with gr.Row():
                        folder = gr.Textbox(
                            label="Stapelordner", placeholder="Ordner auswählen …", scale=5
                        )
                        choose_folder = gr.Button("Ordner auswählen", scale=1)
                    choose_folder.click(controller.pick_folder, outputs=folder)
                with gr.Column(scale=2):
                    year = gr.Number(
                        label="Jahr (optional)", minimum=800, maximum=2100, precision=0
                    )
                    script = gr.Dropdown(
                        ["Automatisch", "Handschrift", "Druck/Fraktur", "Schreibmaschine"],
                        value="Automatisch",
                        label="Schriftart",
                    )
                    advanced = gr.Checkbox(
                        value=True, label="Erweiterte freie lokale Modelle verwenden"
                    )
                    cloud = gr.Checkbox(
                        value=False, label="Unsichere Stellen optional über OpenRouter prüfen"
                    )
                    budget = gr.Slider(
                        0, 10, value=1, step=0.25, label="Maximales Cloud-Budget in USD"
                    )
            start = gr.Button("Entzifferung starten", variant="primary", size="lg")
            status = gr.Markdown(elem_classes=["sl-note"])
            output_files = gr.File(label="Erzeugte Dateien", file_count="multiple")
            start.click(
                controller.process,
                inputs=[uploaded, folder, year, script, advanced, cloud, budget],
                outputs=[status, output_files],
            )

        with gr.Tab("Archivsuche"):
            result_state = gr.State([])
            selected_line = gr.State("")
            with gr.Row():
                query = gr.Textbox(
                    label="Suche", placeholder="Name, Begriff oder Beschreibung …", scale=5
                )
                mode = gr.Dropdown(
                    ["Intelligent", "Exakt", "Namen", "Bedeutung"],
                    value="Intelligent",
                    label="Suchmodus",
                    scale=2,
                )
                search_button = gr.Button("Suchen", variant="primary", scale=1)
            with gr.Row():
                fuzziness = gr.Slider(0.45, 0.98, value=0.72, step=0.01, label="Mindestähnlichkeit")
                year_from = gr.Number(label="Jahr von", precision=0)
                year_to = gr.Number(label="Jahr bis", precision=0)
            search_note = gr.Markdown()
            table = gr.Dataframe(
                headers=["Dokument", "Jahr", "Seite", "Text", "Trefferart", "Treffer", "OCR"],
                datatype=["str", "str", "number", "str", "str", "str", "str"],
                interactive=False,
                wrap=True,
                label="Treffer in allen Dokumenten",
            )
            search_button.click(
                controller.search,
                inputs=[query, mode, fuzziness, year_from, year_to],
                outputs=[table, result_state, search_note],
            )
            query.submit(
                controller.search,
                inputs=[query, mode, fuzziness, year_from, year_to],
                outputs=[table, result_state, search_note],
            )
            with gr.Row():
                scan_preview = gr.Image(label="Fundstelle im Scan", type="pil")
                with gr.Column():
                    hit_details = gr.Markdown()
                    correction = gr.Textbox(label="Transkription korrigieren", lines=5)
                    save_correction = gr.Button("Korrektur speichern")
                    correction_status = gr.Markdown()
                    gnd_button = gr.Button("Ausgewählten Namen optional mit GND abgleichen")
                    gnd_results = gr.Markdown()
            table.select(
                controller.select_hit,
                inputs=[result_state],
                outputs=[scan_preview, hit_details, correction, selected_line],
            )
            save_correction.click(
                controller.save_correction,
                inputs=[selected_line, correction],
                outputs=correction_status,
            )
            gnd_button.click(controller.lookup_gnd, inputs=correction, outputs=gnd_results)

        with gr.Tab("Modelle & Datenschutz"):
            gr.Markdown(
                "Modelle werden aus den dokumentierten Originalquellen in den lokalen "
                "Cache geladen. "
                "Scans, Ergebnisse und Suchindex werden niemals auf GitHub gespeichert."
            )
            model_table = gr.Dataframe(
                value=controller.model_status(),
                headers=["Modell", "Einsatz", "Lizenz", "Status"],
                interactive=False,
                wrap=True,
            )
            model_choice = gr.Dropdown(
                choices=[(spec.name, key) for key, spec in MODELS.items()],
                value="party-v4",
                label="Modell installieren",
            )
            install = gr.Button("Ausgewähltes Modell laden")
            install_status = gr.Markdown()
            install.click(
                controller.install_model, inputs=model_choice, outputs=[install_status, model_table]
            )
            gr.Markdown("### OpenRouter (optional)")
            api_key = gr.Textbox(label="API-Schlüssel", type="password")
            save_key = gr.Button("Sicher im macOS-Schlüsselbund speichern")
            key_status = gr.Markdown()
            save_key.click(controller.save_key, inputs=api_key, outputs=key_status)
    return demo


def launch() -> None:
    demo = build_app()
    demo.queue(default_concurrency_limit=1).launch(
        server_name="127.0.0.1",
        server_port=7860,
        share=False,
        inbrowser=True,
        show_error=True,
        css=CSS,
        theme=gr.themes.Soft(),
    )
