@echo off
title Crear Atlas de mi PC.exe
cd /d "%~dp0"
echo Instalando PyInstaller...
py -3 -m pip install --upgrade pyinstaller
if errorlevel 1 goto fallo
echo Compilando...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name "Atlas de mi PC" --add-data "index.html;." --add-data "three.min.js;." --add-data "OrbitControls.js;." ciudad.py
if errorlevel 1 goto fallo
copy /y "dist\Atlas de mi PC.exe" "%~dp0" >nul
echo.
echo Listo: "Atlas de mi PC.exe" esta en esta carpeta.
pause
exit /b 0
:fallo
echo.
echo Algo fallo. Copia el texto de esta ventana y pasaselo a Claude.
pause
