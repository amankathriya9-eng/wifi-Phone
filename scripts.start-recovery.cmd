@echo off
setlocal EnableExtensions
title AK Backup Recovery Environment (Bare-Metal)

echo ============================================================
echo   INITIALIZING AK BARE-METAL RECOVERY ENVIRONMENT
echo ============================================================

:: 1. Initialize Windows PE hardware devices and networking
echo [*] Initializing Windows PE subsystems (wpeinit)...
call wpeinit

:: 2. Allow storage controllers, NVMe, and SATA buses to settle
echo [*] Waiting for storage controller enumeration...
timeout /t 3 /nobreak >nul

:: 3. Configure offline embedded Python environment variables
set "RECOVERY_ROOT=X:\Recovery"
set "PYTHON_HOME=%RECOVERY_ROOT%\Python"
set "PATH=%PYTHON_HOME%;%PYTHON_HOME%\Scripts;%PATH%"

:: Resolve actual Tcl/Tk versioned library directories dynamically
for /d %%D in ("%PYTHON_HOME%\tcl\tcl8*") do set "TCL_LIBRARY=%%D"
for /d %%D in ("%PYTHON_HOME%\tcl\tk8*") do set "TK_LIBRARY=%%D"

:: 4. Verify runtime components exist on RAMDisk
if not exist "%PYTHON_HOME%\python.exe" (
    echo [CRITICAL ERROR] Python executable not found at %PYTHON_HOME%\python.exe.
    goto :fail
)

if not exist "%RECOVERY_ROOT%\AKRecovery.py" (
    echo [CRITICAL ERROR] Recovery application not found at %RECOVERY_ROOT%\AKRecovery.py.
    goto :fail
)

:: 5. Launch interactive recovery application
:: NOTE: The interactive UI requires explicit user selection, cryptographic
:: validation, and preflight confirmation before any physical restore is executed.
echo [*] Launching AKRecovery.py...
cd /d "%RECOVERY_ROOT%"
"%PYTHON_HOME%\python.exe" "%RECOVERY_ROOT%\AKRecovery.py"
set "EXIT_CODE=%ERRORLEVEL%"

if %EXIT_CODE% neq 0 (
    echo.
    echo [!] AKRecovery terminated with exit code %EXIT_CODE%.
) else (
    echo.
    echo [*] AKRecovery session terminated cleanly.
)

goto :prompt_exit

:fail
echo.
echo [CRITICAL ERROR] Failed to start AK Recovery Environment.
echo Review the console log above for diagnostic information.

:prompt_exit
echo.
echo ============================================================
echo   SESSION OPTIONS
echo ============================================================
echo   [1] Restart System
echo   [2] Shut Down System
echo   [3] Open Diagnostic Command Prompt
echo.
choice /c 123 /n /m "Select option (1-3): "
if errorlevel 3 goto :cmd
if errorlevel 2 goto :shutdown
if errorlevel 1 goto :reboot

:reboot
echo [*] Rebooting system...
wpeutil reboot
exit /b 0

:shutdown
echo [*] Shutting down system...
wpeutil shutdown
exit /b 0

:cmd
echo [*] Diagnostic Command Shell active. Type 'exit' to return to menu.
cmd.exe
goto :prompt_exit