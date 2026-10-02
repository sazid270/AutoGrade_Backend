@echo off
setlocal

REM Resolve project root (one level up from the scripts directory)
pushd "%~dp0\.."

REM Create a virtual environment in `.venv-win` if it doesn't already exist
if not exist ".venv-win\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv-win
    if errorlevel 1 (
        python -m venv .venv-win
        if errorlevel 1 (
            echo Failed to create virtual environment.
            popd
            exit /b 1
        )
    )
)

REM Activate the virtual environment
call ".venv-win\Scripts\activate.bat"
if errorlevel 1 (
    popd
    exit /b 1
)

REM Keep packaging tools up-to-date to avoid resolver and SSL issues
echo "Upgrading pip and setuptools..."
python -m pip install --upgrade pip "setuptools<82"

REM Install runtime dependencies if present
if exist requirements.txt (
    echo Installing runtime dependencies...
    python -m pip install -r requirements.txt
)

REM Install development dependencies if present
if exist requirements_dev.txt (
    echo Installing development dependencies...
    python -m pip install -r requirements_dev.txt
)

REM Delete old migrations
if exist authentication\migrations\0*.py del /Q authentication\migrations\0*
if exist authorization\migrations\0*.py del /Q authorization\migrations\0*
if exist app\migrations\0*.py del /Q app\migrations\0*
if exist filesystem\migrations\0*.py del /Q filesystem\migrations\0*

REM Make migrations
echo Making migrations...
python manage.py makemigrations authentication authorization app filesystem

REM Migrate database
echo Migrating database...
python manage.py migrate

REM Remove static files
echo Removing static files...
if exist staticfiles rmdir /S /Q staticfiles

echo Reset completed.

popd
endlocal
