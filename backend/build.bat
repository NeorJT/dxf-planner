@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1
cd /D "%~dp0"
cl /LD /O2 /TC dxf_fast_parser.c /Fe:dxf_fast_parser.dll /link /OUT:dxf_fast_parser.dll
echo DONE
exit /b %ERRORLEVEL%