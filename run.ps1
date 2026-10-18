$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
python main.py
