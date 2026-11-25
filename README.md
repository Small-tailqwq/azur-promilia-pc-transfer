# Azur Promilia PC Transfer

A Windows tool for switching a local Azur Promilia installation between the official PC and Bilibili launchers. Supports client version `1.0.0.30`.

1. Download the ZIP from [Releases](https://github.com/Small-tailqwq/azur-promilia-pc-transfer/releases/latest) and open the EXE.
2. Choose the existing game folder, target launcher, and a new output directory. Enable hard links to reuse large resources on the same volume.
3. In the target launcher, locate the new directory and run its built-in repair before playing.

Per-file SHA-256 verification during transfer is optional. The ZIP includes a short Chinese guide.

## Build

```powershell
python -m PyInstaller --onefile --windowed --noupx --name AzurPromiliaTransfer-Official-Bilibili --add-data 'manifest.json.gz:.' --add-data 'repair-sources.json:.' azur_transfer_gui.pyw
```
