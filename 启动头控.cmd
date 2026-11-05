@echo off
setlocal
title 头动鼠标 HeadMouse - 实验4 体感操控
cd /d "%~dp0"

rem 优先用本机装了 opencv 的解释器；找不到就退回 PATH 里的 python
set "PY=C:\Users\chen\AppData\Local\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk"
chcp 936 >nul

echo ============================================================
echo   HeadMouse 头动鼠标  --  免安装版 eViacam
echo   1 等摄像头窗口弹出（能看到黄色人脸框）
echo   2 按 空格 开始：光标原点就是按下时的位置，不会跳到屏幕中心
echo   3 头往哪偏光标往哪走；停住 0.8 秒 = 单击
echo   4 X 单击/双击   T 位置/速度   C 重新锁定   O 重设原点
echo   5 空格 暂停   F8 暂停(全局生效)   F12 退出(全局生效)   ESC 退出
echo ============================================================
echo.

"%PY%" -c "import cv2, numpy" >nul 2>&1
if errorlevel 1 goto nolib

if not exist "head_mouse.py" goto noscript

echo [启动] 解释器: %PY%
echo [启动] 目录:   %CD%
echo.
"%PY%" "head_mouse.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo [结束] 返回码 %RC%   0 表示正常退出
pause
exit /b 0

:nolib
echo [错误] 当前 Python 没有 opencv / numpy，请先执行：
echo    "%PY%" -m pip install opencv-python numpy
echo.
pause
exit /b 1

:noscript
echo [错误] 当前目录找不到 head_mouse.py
echo        请确认本 .cmd 与 head_mouse.py 在同一文件夹（当前: %CD%）
echo.
pause
exit /b 1
