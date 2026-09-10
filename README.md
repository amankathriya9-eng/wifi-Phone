# \# AK Backup Recovery Environment (WinPE ISO)

# 

# This repository contains the automated build pipeline for the \*\*AK Backup Recovery Environment\*\*, producing a bootable, standalone, dual-mode (BIOS + UEFI) Windows PE (WinPE) ISO that embeds `AKRecovery.py`.

# 

# The generated ISO provides an offline recovery environment capable of parsing AKBK v2 backups, decrypting system manifests and block indexes, resolving differential chains, verifying sector-level read-back hashes, and repairing UEFI/BIOS boot configurations directly against physical storage media.

# 

# \---

# 

# \## 1. Repository Structure

# 

# ```text

# .

# ├── .github/

# │   └── workflows/

# │       └── build-iso.yml        # CI pipeline (Runner tests, ADK setup, ISO build)

# ├── scripts/

# │   ├── build\_iso.ps1            # Staging and ISO assembly script (DISM + Oscdimg)

# │   └── start-recovery.cmd       # WinPE boot supervisor script

# ├── AKRecovery.py                # Authoritative Bare-Metal Recovery Application

# ├── requirements.txt             # Strict offline runtime dependencies

# └── README.md                    # Operational and build documentation

