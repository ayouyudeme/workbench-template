@echo off
cd /d "%~dp0"
set "LOG=%~dp0start.log"
echo [%TIME%] start >> "%LOG%"

where python >nul 2>nul && set "PY=python"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY goto nopython

echo ============================================
echo   工作台界面
echo   地址 http://127.0.0.1:8765/
echo   （端口被占用会自动往后找一个，下面打印实际地址）
echo   关掉这个窗口 = 停止界面
echo ============================================
echo.

echo 先跑一遍体检...
%PY% "%~dp0..\体检\体检.py" --no-write >nul 2>nul
set "HC=%ERRORLEVEL%"
chcp 936 >nul 2>nul
if not "%HC%"=="0" (
  title 工作台 - 体检没通过
  echo [!!] 体检没通过：点界面右上角体检标签看是哪项红了。
) else (
  title 工作台
  echo [OK] 体检通过。
)
echo.

%PY% "%~dp0server.py"
echo.
echo 界面已退出。日志：%~dp0start.log
pause
exit /b

:nopython
echo 没找到 python。装一个，或把 python.exe 的完整路径填进本文件的 PY 变量。
pause
exit /b 1
