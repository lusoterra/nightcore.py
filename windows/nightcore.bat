@echo off
rem nightcore command: installed tree root.
setlocal
set "HERE=%~dp0"
set "PATH=%HERE%tools;%HERE%python;%PATH%"
rem -s: no user site, -E: ignore PYTHON* env, -u: unbuffered
"%HERE%python\python.exe" -s -E -u -m nightcore %*
