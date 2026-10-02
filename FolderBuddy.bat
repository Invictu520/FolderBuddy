@echo off
rem FolderBuddy per Doppelklick starten
cd /d "%~dp0"
where pyw >NUL 2>NUL && (start "" pyw gui.py & exit /b 0)
where pythonw >NUL 2>NUL && (start "" pythonw gui.py & exit /b 0)
echo Python wurde nicht gefunden.
echo Bitte von https://www.python.org/downloads/ installieren
echo und beim Installieren "Add python.exe to PATH" ankreuzen.
pause
