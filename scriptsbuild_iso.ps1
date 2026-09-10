<#
.SYNOPSIS
    Authoritative Build Script for AK Recovery Bare-Metal WinPE ISO.
.DESCRIPTION
    Extracts an isolated Python 3.11.x x64 runtime with complete Tkinter subsystem,
    builds an offline pip wheelhouse, installs packages into the embedded environment,
    injects WinPE Optional Components, stages AKRecovery.py and startup supervisor,
    executes an in-mount self-verification using the embedded interpreter, and
    generates a bootable dual-mode (BIOS + UEFI) ISO using oscdimg.
#>
[CmdletBinding()]
param (
    [string]$PythonVersion = "3.11.9",
    [string]$OutputFile = "AKRecovery_Environment.iso"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-BuildLog {
    param(
        [Parameter(Mandatory=$true)][string]$Message,
        [string]$Color = "Cyan"
    )
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] [BUILD] $Message" -ForegroundColor $Color
}

function Write-BuildSuccess {
    param([Parameter(Mandatory=$true)][string]$Message)
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] [SUCCESS] $Message" -ForegroundColor Green
}

function Write-BuildFatal {
    param([Parameter(Mandatory=$true)][string]$Message)
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] [FATAL] $Message" -ForegroundColor Red
    throw $Message
}

function Assert-ValidPath {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [Parameter(Mandatory=$true)][string]$Description
    )
    if (-not (Test-Path -Path $Path)) {
        Write-BuildFatal "Verification failed: $Description was not found at expected path: $Path"
    }
}

function Download-FileWithCheck {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [Parameter(Mandatory=$true)][string]$DestinationPath,
        [int64]$MinimumSizeBytes = 1048576
    )
    Write-BuildLog "Downloading: $Uri -> $DestinationPath"
    $parentDir = Split-Path -Parent $DestinationPath
    if (-not (Test-Path -Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    $ProgressPreference = 'SilentlyContinue'
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12 -bor [System.Net.SecurityProtocolType]::Tls13
    Invoke-WebRequest -Uri $Uri -OutFile $DestinationPath -UseBasicParsing

    Assert-ValidPath -Path $DestinationPath -Description "Downloaded target file"
    $fileItem = Get-Item -Path $DestinationPath
    if ($fileItem.Length -lt $MinimumSizeBytes) {
        Write-BuildFatal "Downloaded file '$DestinationPath' is too small ($($fileItem.Length) bytes). Download failed or returned error page."
    }
}

# ---------------------------------------------------------------------
# 1. Resolve and Verify Source Repository Paths
# ---------------------------------------------------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Get-Item -Path $ScriptDir).Parent.FullName

$AKRecoverySrc   = Join-Path -Path $RepoRoot -ChildPath "AKRecovery.py"
$RequirementsSrc = Join-Path -Path $RepoRoot -ChildPath "requirements.txt"
$StartupCmdSrc   = Join-Path -Path $ScriptDir -ChildPath "start-recovery.cmd"

Assert-ValidPath -Path $AKRecoverySrc -Description "AKRecovery.py application source"
Assert-ValidPath -Path $RequirementsSrc -Description "requirements.txt"
Assert-ValidPath -Path $StartupCmdSrc -Description "start-recovery.cmd startup supervisor"

if (-not [System.IO.Path]::IsPathRooted($OutputFile)) {
    $FinalIsoPath = Join-Path -Path $RepoRoot -ChildPath $OutputFile
} else {
    $FinalIsoPath = $OutputFile
}

# ---------------------------------------------------------------------
# 2. Locate Windows ADK & WinPE Add-on Deployments
# ---------------------------------------------------------------------
Write-BuildLog "Locating Windows ADK and WinPE toolchain..."

$AdkSearchRoots = @(
    "${env:ProgramFiles(x86)}\Windows Kits\10\Assessment and Deployment Kit",
    "$env:ProgramFiles\Windows Kits\10\Assessment and Deployment Kit"
)

$RegKeys = @(
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots",
    "HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots"
)

foreach ($reg in $RegKeys) {
    if (Test-Path -Path $reg) {
        $kitsRoot = (Get-ItemProperty -Path $reg -ErrorAction SilentlyContinue).KitsRoot10
        if ($kitsRoot) {
            $AdkSearchRoots += (Join-Path -Path $kitsRoot -ChildPath "Assessment and Deployment Kit")
        }
    }
}

$AdkRoot = $null
foreach ($candidate in $AdkSearchRoots) {
    if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
    $depCheck = Join-Path -Path $candidate -ChildPath "Deployment Tools"
    $peCheck  = Join-Path -Path $candidate -ChildPath "Windows Preinstallation Environment"
    if ((Test-Path -Path $depCheck) -and (Test-Path -Path $peCheck)) {
        $AdkRoot = $candidate
        break
    }
}

if (-not $AdkRoot) {
    Write-BuildFatal "Unable to locate a matching Windows ADK and WinPE Add-on installation."
}

Write-BuildLog "Discovered ADK Root: $AdkRoot"
$DeploymentTools = Join-Path -Path $AdkRoot -ChildPath "Deployment Tools"
$WinpeBase       = Join-Path -Path $AdkRoot -ChildPath "Windows Preinstallation Environment"

$OscdimgExe   = Join-Path -Path $DeploymentTools -ChildPath "amd64\Oscdimg\oscdimg.exe"
$EtfsbootCom  = Join-Path -Path $DeploymentTools -ChildPath "amd64\Oscdimg\etfsboot.com"
$EfisysBin    = Join-Path -Path $DeploymentTools -ChildPath "amd64\Oscdimg\efisys.bin"
$WinpeAmd64   = Join-Path -Path $WinpeBase -ChildPath "amd64"
$WinpeWimSrc  = Join-Path -Path $WinpeAmd64 -ChildPath "en-us\winpe.wim"
$WinpeMedia   = Join-Path -Path $WinpeAmd64 -ChildPath "Media"
$WinpeOcsDir  = Join-Path -Path $WinpeAmd64 -ChildPath "WinPE_OCs"
$DismExe      = "dism.exe"

Assert-ValidPath -Path $OscdimgExe -Description "oscdimg.exe"
Assert-ValidPath -Path $EtfsbootCom -Description "etfsboot.com (BIOS boot code)"
Assert-ValidPath -Path $EfisysBin -Description "efisys.bin (UEFI boot code)"
Assert-ValidPath -Path $WinpeWimSrc -Description "Base winpe.wim"
Assert-ValidPath -Path $WinpeMedia -Description "Base WinPE Media directory"
Assert-ValidPath -Path $WinpeOcsDir -Description "WinPE_OCs package directory"

# ---------------------------------------------------------------------
# 3. Prepare Clean Workspace
# ---------------------------------------------------------------------
$WorkspaceRoot = Join-Path -Path $env:TEMP -ChildPath "AKRecovery_BuildWorkspace_$([guid]::NewGuid().ToString('N'))"
$MediaStaging  = Join-Path -Path $WorkspaceRoot -ChildPath "media"
$MountDir      = Join-Path -Path $WorkspaceRoot -ChildPath "mount"
$TargetWim     = Join-Path -Path $MediaStaging -ChildPath "sources\boot.wim"

Write-BuildLog "Setting up clean workspace at: $WorkspaceRoot"
New-Item -ItemType Directory -Path $MediaStaging -Force | Out-Null
New-Item -ItemType Directory -Path $MountDir -Force | Out-Null

Write-BuildLog "Staging media layout and copying winpe.wim..."
Copy-Item -Path "$WinpeMedia\*" -Destination $MediaStaging -Recurse -Force
$SourcesDir = Join-Path -Path $MediaStaging -ChildPath "sources"
if (-not (Test-Path -Path $SourcesDir)) {
    New-Item -ItemType Directory -Path $SourcesDir -Force | Out-Null
}
Copy-Item -Path $WinpeWimSrc -Destination $TargetWim -Force

# ---------------------------------------------------------------------
# 4. Mount boot.wim with DISM
# ---------------------------------------------------------------------
Write-BuildLog "Mounting boot.wim..."
$dismMount = & $DismExe /Mount-Image /ImageFile:"$TargetWim" /Index:1 /MountDir:"$MountDir"
if ($LASTEXITCODE -ne 0) {
    Write-BuildFatal "DISM mount failed. Output: $($dismMount -join "`n")"
}
$WimMounted =$true

try {
    # -----------------------------------------------------------------
    # 5. Inject WinPE Optional Components
    # -----------------------------------------------------------------
    Write-BuildLog "Injecting required WinPE Optional Components..."
    $RequiredCabs = @(
        "WinPE-Scripting.cab",
        "en-us\WinPE-Scripting_en-us.cab",
        "WinPE-WMI.cab",
        "en-us\WinPE-WMI_en-us.cab",
        "WinPE-EnhancedStorage.cab",
        "en-us\WinPE-EnhancedStorage_en-us.cab",
        "WinPE-StorageWMI.cab",
        "en-us\WinPE-StorageWMI_en-us.cab"
    )

    foreach ($cabName in $RequiredCabs) {$cabFullPath = Join-Path -Path $WinpeOcsDir -ChildPath$cabName
        if (-not (Test-Path -Path $cabFullPath)) {
            $availableMatching = (Get-ChildItem -Path$WinpeOcsDir -Filter "*$([System.IO.Path]::GetFileNameWithoutExtension($cabName))*").Name
            Write-BuildFatal "Requested CAB '$cabName' missing in$WinpeOcsDir. Matching available: $($availableMatching -join ', ')"
        }
        Write-BuildLog "  Adding package: $cabName"
        $pkgOut = &$DismExe /Image:"$MountDir" /Add-Package /PackagePath:"$cabFullPath" /NoRestart
        if ($LASTEXITCODE -ne 0) {
            Write-BuildFatal "DISM failed to add package $cabName. Output: $($pkgOut -join "`n")"
        }
    }

    Write-BuildLog "Setting WinPE Scratch Space to 512 MB..."
    & $DismExe /Image:"$MountDir" /Set-ScratchSpace:512 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-BuildFatal "Failed to set WinPE scratch space."
    }

    # -----------------------------------------------------------------
    # 6. Extract Complete Standalone Python 3.11 Runtime + Tkinter
    # -----------------------------------------------------------------
    Write-BuildLog "Acquiring official Python $PythonVersion installer layout..."
    $InstallerFileName = "python-$PythonVersion-amd64.exe"
    $InstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/$InstallerFileName"
    $InstallerLocalPath = Join-Path -Path $env:TEMP -ChildPath $InstallerFileName

    if (-not (Test-Path -Path $InstallerLocalPath)) {
        Download-FileWithCheck -Uri $InstallerUrl -DestinationPath $InstallerLocalPath -MinimumSizeBytes 25000000
    }

    $PyLayoutDir  = Join-Path -Path $WorkspaceRoot -ChildPath "py_layout"
    $PyStagingDir = Join-Path -Path $WorkspaceRoot -ChildPath "py_staging"

    New-Item -ItemType Directory -Path $PyLayoutDir -Force | Out-Null
    New-Item -ItemType Directory -Path $PyStagingDir -Force | Out-Null

    Write-BuildLog "Extracting installer layout with /layout..."
    $layoutProc = Start-Process -FilePath $InstallerLocalPath -ArgumentList "/quiet", "/layout", "`"$PyLayoutDir`"" -Wait -PassThru -NoNewWindow
    if ($layoutProc.ExitCode -ne 0) {
        Write-BuildFatal "Python installer layout extraction failed with code: $($layoutProc.ExitCode)"
    }

    # Extract required MSIs: core, lib, tcltk, and tools (pip)
    $TargetMsis = @("core.msi", "lib.msi", "tcltk.msi", "tools.msi")
    foreach ($msi in $TargetMsis) {
        $msiPath = Join-Path -Path $PyLayoutDir -ChildPath $msi
        Assert-ValidPath -Path $msiPath -Description "Extracted layout package $msi"
        Write-BuildLog "  Unpacking administrative image: $msi..."
        $msiProc = Start-Process -FilePath "msiexec.exe" -ArgumentList "/a", "`"$msiPath`"", "/qn", "TARGETDIR=`"$PyStagingDir`"" -Wait -PassThru -NoNewWindow
        if ($msiProc.ExitCode -ne 0) {
            Write-BuildFatal "msiexec failed extracting $msi with exit code: $($msiProc.ExitCode)"
        }
    }

    # Deploy Python into X:\Recovery\Python on the WinPE target
    $TargetRecoveryDir = Join-Path -Path $MountDir -ChildPath "Recovery"
    $TargetPythonDir   = Join-Path -Path $TargetRecoveryDir -ChildPath "Python"
    New-Item -ItemType Directory -Path $TargetPythonDir -Force | Out-Null

    Write-BuildLog "Copying complete Python runtime to $TargetPythonDir..."
    Copy-Item -Path "$PyStagingDir\*" -Destination $TargetPythonDir -Recurse -Force

    # Configure deterministic python311._pth inside embedded target
    $PthPath = Join-Path -Path $TargetPythonDir -ChildPath "python311._pth"
    $PthContent = @(
        ".",
        "DLLs",
        "Lib",
        "Lib\site-packages",
        "import site"
    )
    Set-Content -Path $PthPath -Value $PthContent -Encoding ASCII

    # -----------------------------------------------------------------
    # 7. Build Offline Wheelhouse and Install Dependencies
    # -----------------------------------------------------------------
    Write-BuildLog "Building offline wheelhouse using runner pip..."
    $WheelhouseDir = Join-Path -Path $WorkspaceRoot -ChildPath "wheelhouse"
    New-Item -ItemType Directory -Path $WheelhouseDir -Force | Out-Null

    # Download pre-compiled binary wheels matching CPython 3.11 win_amd64
    $dlProc = Start-Process -FilePath "python.exe" -ArgumentList "-m", "pip", "download", "--dest", "`"$WheelhouseDir`"", "--only-binary=:all:", "--python-version", "311", "--platform", "win_amd64", "--implementation", "cp", "-r", "`"$RequirementsSrc`"" -Wait -PassThru -NoNewWindow
    if ($dlProc.ExitCode -ne 0) {
        Write-BuildFatal "Failed to download offline wheels for requirements.txt."
    }

    Write-BuildLog "Installing wheels into WinPE embedded Python site-packages..."
    $TargetSitePackages = Join-Path -Path $TargetPythonDir -ChildPath "Lib\site-packages"
    New-Item -ItemType Directory -Path $TargetSitePackages -Force | Out-Null

    # Execute pip install pointing to the local wheelhouse with --no-index
    $instProc = Start-Process -FilePath "python.exe" -ArgumentList "-m", "pip", "install", "--no-index", "--find-links=`"$WheelhouseDir`"", "--target=`"$TargetSitePackages`"", "-r", "`"$RequirementsSrc`"" -Wait -PassThru -NoNewWindow
    if ($instProc.ExitCode -ne 0) {
        Write-BuildFatal "Failed to install offline wheels into WinPE Python site-packages."
    }

    # Resolve pywin32 system DLL locations for portable environment
    $Pywin32Sys32Dir = Join-Path -Path $TargetSitePackages -ChildPath "pywin32_system32"
    if (Test-Path -Path $Pywin32Sys32Dir) {
        Write-BuildLog "Deploying pywin32 core DLLs to WinPE System32 and Python root..."
        Get-ChildItem -Path $Pywin32Sys32Dir -Filter "*.dll" | ForEach-Object {
            Copy-Item -Path $_.FullName -Destination $TargetPythonDir -Force
            Copy-Item -Path $_.FullName -Destination (Join-Path -Path $MountDir -ChildPath "Windows\System32") -Force
        }
    }

    # -----------------------------------------------------------------
    # 8. Stage Application and Setup WinPE Boot Startup
    # -----------------------------------------------------------------
    Write-BuildLog "Staging AKRecovery.py and startup supervisor scripts..."
    Copy-Item -Path $AKRecoverySrc -Destination (Join-Path -Path $TargetRecoveryDir -ChildPath "AKRecovery.py") -Force

    $WinpeSystem32 = Join-Path -Path $MountDir -ChildPath "Windows\System32"
    Copy-Item -Path $StartupCmdSrc -Destination (Join-Path -Path $WinpeSystem32 -ChildPath "start-recovery.cmd") -Force

    # Configure startnet.cmd: Execute start-recovery.cmd (which calls wpeinit exactly once)
    $StartnetPath = Join-Path -Path $WinpeSystem32 -ChildPath "startnet.cmd"
    $StartnetScript = @"
@echo off
call %WINDIR%\System32\start-recovery.cmd
"@
    Set-Content -Path $StartnetPath -Value $StartnetScript -Encoding ASCII

    # -----------------------------------------------------------------
    # 9. In-Mount Verification of Offline Runtime Dependencies
    # -----------------------------------------------------------------
    Write-BuildLog "Executing mandatory in-mount verification using embedded interpreter..."
    
    # Locate staged Tcl/Tk directories for runtime configuration
    $TclDir = Join-Path -Path $TargetPythonDir -ChildPath "tcl"
    $TclLibDir = Get-ChildItem -Path $TclDir -Directory -Filter "tcl8*" -ErrorAction SilentlyContinue | Select-Object -First 1
    $TkLibDir  = Get-ChildItem -Path $TclDir -Directory -Filter "tk8*" -ErrorAction SilentlyContinue | Select-Object -First 1

    if (-not $TclLibDir -or -not $TkLibDir) {
        Write-BuildFatal "Tcl/Tk library directory structure is missing or unrecognized inside $TclDir."
    }

    $InMountScript = @"
import os
import sys

# Configure Tcl/Tk variables pointing to the staged directories on build host
os.environ['TCL_LIBRARY'] = r'$($TclLibDir.FullName)'
os.environ['TK_LIBRARY']  = r'$($TkLibDir.FullName)'

# 1. Test Tkinter
import tkinter as tk
root = tk.Tk()
root.withdraw()
root.destroy()

# 2. Test Cryptography
import cryptography
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import ec

# 3. Test PyWin32
import win32api
import win32file
import win32security
import win32com.client

# 4. Test WMI and execute safe read query
import wmi
c = wmi.WMI()
disks = c.Win32_DiskDrive()

print("ALL_OFFLINE_MODULES_LOADED_SUCCESSFULLY")
sys.exit(0)
"@

    $VerifyPyPath = Join-Path -Path $WorkspaceRoot -ChildPath "verify_runtime.py"
    Set-Content -Path $VerifyPyPath -Value $InMountScript -Encoding ASCII

    $EmbeddedPyExe = Join-Path -Path $TargetPythonDir -ChildPath "python.exe"
    $VerifyStdOut = Join-Path -Path $WorkspaceRoot -ChildPath "verify_stdout.log"
    $VerifyStdErr = Join-Path -Path $WorkspaceRoot -ChildPath "verify_stderr.log"

    $verifyProc = Start-Process -FilePath $EmbeddedPyExe -ArgumentList "`"$VerifyPyPath`"" -Wait -PassThru -NoNewWindow -RedirectStandardOutput $VerifyStdOut -RedirectStandardError $VerifyStdErr
    $outText = Get-Content -Path $VerifyStdOut -ErrorAction SilentlyContinue
    $errText = Get-Content -Path $VerifyStdErr -ErrorAction SilentlyContinue

    if ($verifyProc.ExitCode -ne 0 -or ($outText -notcontains "ALL_OFFLINE_MODULES_LOADED_SUCCESSFULLY")) {
        Write-Host "--- VERIFICATION ERROR DIAGNOSTIC ---" -ForegroundColor Red
        Write-Host "STDOUT:`n$($outText -join "`n")" -ForegroundColor Yellow
        Write-Host "STDERR:`n$($errText -join "`n")" -ForegroundColor Red
        Write-BuildFatal "Embedded Python runtime verification failed inside staged filesystem."
    }
    Write-BuildSuccess "In-mount verification passed: Tkinter, Cryptography, PyWin32, and WMI initialized cleanly."

    # Verify key payload files exist before unmounting
    Assert-ValidPath -Path (Join-Path -Path $TargetRecoveryDir -ChildPath "AKRecovery.py") -Description "Target AKRecovery.py"
    Assert-ValidPath -Path (Join-Path -Path $TargetPythonDir -ChildPath "python.exe") -Description "Target python.exe"
    Assert-ValidPath -Path (Join-Path -Path $WinpeSystem32 -ChildPath "startnet.cmd") -Description "Target startnet.cmd"
    Assert-ValidPath -Path (Join-Path -Path $WinpeSystem32 -ChildPath "start-recovery.cmd") -Description "Target start-recovery.cmd"

    # -----------------------------------------------------------------
    # 10. Commit and Unmount boot.wim
    # -----------------------------------------------------------------
    Write-BuildLog "Committing changes and unmounting boot.wim..."
    $unmountOut = & $DismExe /Unmount-Image /MountDir:"$MountDir" /Commit
    if ($LASTEXITCODE -ne 0) {
        Write-BuildFatal "DISM unmount commit failed. Output: $($unmountOut -join "`n")"
    }
    $WimMounted =$false
    Write-BuildSuccess "boot.wim successfully committed and closed."

} catch {
    Write-Host "BUILD ABORTED WITH ERROR: $_" -ForegroundColor Red
    if ($WimMounted) {
        Write-BuildLog "Discarding WIM modifications..." "Yellow"
        & $DismExe /Unmount-Image /MountDir:"$MountDir" /Discard | Out-Null
    }
    throw $_
}

# ---------------------------------------------------------------------
# 11. Generate Bootable Dual-Boot (BIOS + UEFI) ISO with oscdimg
# ---------------------------------------------------------------------
Write-BuildLog "Generating dual-mode BIOS/UEFI bootable ISO with oscdimg..."

if (Test-Path -Path $FinalIsoPath) {
    Remove-Item -Path $FinalIsoPath -Force
}

# El Torito multi-boot specification:
# bootdata:2
#   #p0,e,b<etfsboot.com>  -> Platform 0 (BIOS), uncompressed, boot code
#   #pEF,e,b<efisys.bin>   -> Platform 0xEF (UEFI), uncompressed, boot code
$BootDataParam = "2#p0,e,b`"$EtfsbootCom`"#pEF,e,b`"$EfisysBin`""

$OscdArgs = @(
    "-m",
    "-o",
    "-u2",
    "-udfver102",
    "-bootdata:$BootDataParam",
    "`"$MediaStaging`"",
    "`"$FinalIsoPath`""
)

$oscdProc = Start-Process -FilePath $OscdimgExe -ArgumentList$OscdArgs -Wait -PassThru -NoNewWindow
if ($oscdProc.ExitCode -ne 0 -or (-not (Test-Path -Path$FinalIsoPath))) {
    Write-BuildFatal "oscdimg failed to create output ISO (Exit code: $($oscdProc.ExitCode))."
}

# ---------------------------------------------------------------------
# 12. Final ISO Verification & Cleanup
# ---------------------------------------------------------------------
$IsoItem = Get-Item -Path$FinalIsoPath
$IsoSizeMB = [math]::Round(($IsoItem.Length / 1MB), 2)

if ($IsoItem.Length -lt 250MB) {
    Write-BuildFatal "Created ISO ($IsoSizeMB MB) is smaller than expected size for a complete WinPE+Python image."
}

Write-BuildLog "Cleaning up staging workspace..."
Remove-Item -Path $WorkspaceRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-BuildSuccess "============================================================"
Write-BuildSuccess " RECOVERY ISO BUILD COMPLETED SUCCESSFULLY"
Write-BuildSuccess " Output Artifact: $FinalIsoPath"
Write-BuildSuccess " File Size:       $IsoSizeMB MB"
Write-BuildSuccess " Target Firmware: Dual Boot (BIOS + UEFI x64)"
Write-BuildSuccess "============================================================"