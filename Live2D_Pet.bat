@echo off
title Live2D桌宠启动器
cd /d "%~dp0"
call venv\Scripts\activate.bat
set XLLAMA_SPEC_MODE=mtp
set GGML_VK_ENABLE_MEMORY_PRIORITY=1
set LLAMA_ARG_NO_SLEEP=1
python Live2D_Pet.py
pause