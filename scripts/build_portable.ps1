$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

python -m PyInstaller `
  --noconsole `
  --onefile `
  -y `
  --name "PDFMerger" `
  --icon "gui\\assets\\app_icon.ico" `
  --add-data "gui\\assets\\app_icon.ico;gui\\assets" `
  main.py
