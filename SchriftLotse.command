#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "uv wird über Homebrew installiert …"
    brew install uv
  else
    echo "Bitte zuerst Homebrew oder uv installieren: https://docs.astral.sh/uv/"
    read -k 1 "?Taste drücken zum Beenden …"
    exit 1
  fi
fi

if ! command -v tesseract >/dev/null 2>&1; then
  echo "Für den sofortigen OCR-Fallback wird Tesseract benötigt."
  read "answer?Tesseract und deutsche Sprachdaten jetzt über Homebrew installieren? [J/n] "
  if [[ -z "$answer" || "$answer" == [JjYy]* ]]; then
    brew install tesseract tesseract-lang
  fi
fi

echo "SchriftLotse wird vorbereitet …"
uv sync --frozen --extra models --extra mlx

if ! uv run schriftlotse models core-ready >/dev/null 2>&1; then
  echo "Für historische Schriften und die Bedeutungssuche werden die freien lokalen Kernmodelle benötigt (ca. 4,2 GB)."
  read "models_answer?Empfohlene freie Kernmodelle jetzt lokal installieren? [J/n] "
  if [[ -z "$models_answer" || "$models_answer" == [JjYy]* ]]; then
    uv run schriftlotse models install-core
  fi
fi

if ! uv run schriftlotse models best-ready >/dev/null 2>&1; then
  echo "Für die beste lokale Qualität kann CHURRO 3B MLX 8-Bit installiert werden (ca. 4,4 GB)."
  echo "Die Modellgewichte stehen unter der Qwen Research License und sind für Forschung/nichtkommerzielle Nutzung gedacht."
  read "churro_answer?Lizenz bestätigen und CHURRO als lokalen Standard installieren? [J/n] "
  if [[ -z "$churro_answer" || "$churro_answer" == [JjYy]* ]]; then
    uv run schriftlotse models install-best --accept-research-license
  fi
fi

echo "SchriftLotse startet lokal im Browser …"
echo "🔒 Nur dieser Mac: http://127.0.0.1:7860 (keine öffentliche Freigabe)"
uv run schriftlotse gui
