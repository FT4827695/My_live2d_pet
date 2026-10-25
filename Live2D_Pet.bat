@echo off
title Live2D桌宠启动器
cd /d "%~dp0"
call venv\Scripts\activate.bat
set XLLAMA_SPEC_MODE=mtp
python Live2D_Pet.py
pause