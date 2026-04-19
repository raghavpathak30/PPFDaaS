#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$ROOT_DIR/data"
CSV_PATH="$DATA_DIR/creditcard.csv"
ZIP_PATH="$DATA_DIR/creditcardfraud.zip"

mkdir -p "$DATA_DIR"

if [[ -f "$CSV_PATH" ]]; then
  echo "Dataset already present: $CSV_PATH"
  exit 0
fi

if command -v kaggle >/dev/null 2>&1; then
  echo "Kaggle CLI detected. Attempting automatic download..."
  kaggle datasets download -d mlg-ulb/creditcardfraud -p "$DATA_DIR"

  if [[ -f "$ZIP_PATH" ]]; then
    unzip -o "$ZIP_PATH" -d "$DATA_DIR"
  fi

  if [[ -f "$CSV_PATH" ]]; then
    echo "Dataset ready: $CSV_PATH"
    exit 0
  fi

  echo "Download attempted, but $CSV_PATH was not found after extraction."
  echo "Please place the CSV manually at: $CSV_PATH"
  exit 1
fi

echo "Kaggle CLI not found."
echo "Manual setup required:"
echo "1. Download the ULB credit card fraud dataset (creditcard.csv)."
echo "2. Place it at: $CSV_PATH"
echo "3. Re-run your pipeline commands."
exit 1
