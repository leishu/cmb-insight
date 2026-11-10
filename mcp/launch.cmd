@echo off
rem cmb-insight MCP 启动器（Windows）。
rem 探测顺序与应用内的 launch.sh 一致：CMB_PYTHON -> py launcher -> python。
rem 注意：这个版本没有构建机的 Windows 环境可验证，首次使用请先手动跑一遍。
setlocal
set "HERE=%~dp0"

if defined CMB_PYTHON (
    "%CMB_PYTHON%" "%HERE%server.py" %*
    exit /b %errorlevel%
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%HERE%server.py" %*
    exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%HERE%server.py" %*
    exit /b %errorlevel%
)

echo [cmb-insight] 找不到可用的 Python 解释器，请设置 CMB_PYTHON 环境变量 1>&2
exit /b 1
