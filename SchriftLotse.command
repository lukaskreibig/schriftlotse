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
uv sync --extra models

if ! uv run schriftlotse models core-ready >/dev/null 2>&1; then
  echo "Für schwierige historische Handschrift werden Party v4 und Orli empfohlen (ca. 1,1 GB)."
  read "models_answer?Empfohlene freie Kernmodelle jetzt lokal installieren? [J/n] "
  if [[ -z "$models_answer" || "$models_answer" == [JjYy]* ]]; then
    uv run schriftlotse models install-core
  fi
fi

echo "SchriftLotse startet lokal im Browser …"
uv run schriftlotse gui
