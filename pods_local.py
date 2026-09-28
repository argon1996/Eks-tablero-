#!/usr/bin/env python3
"""Entrada compatible: py pods_local.py [--demo | --connect-script RUTA]."""
import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import json
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path
from eks_console import backend, server

APP_ID = 'bancolombia-eks-console'


def existing_instance_url(port, attempts=1):
    """Devuelve la URL solo si el puerto pertenece a otra EKS Console."""
    url = f'http://127.0.0.1:{port}'
    for attempt in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(url + '/api/health', timeout=.7) as response:
                data = json.loads(response.read(4096).decode('utf-8'))
            if data.get('app') == APP_ID:
                return url
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if attempt + 1 < attempts:
            time.sleep(.15)
    return ''


def reopen_existing(url, no_browser=False):
    print(f'EKS Console ya está abierta. Reutilizando la sesión activa:\n{url}', flush=True)
    if not no_browser:
        webbrowser.open(url)
    return 0

def connect_and_launch(script, args):
    if script == 'auto':
        candidates = backend.discover_scripts()
        if len(candidates) != 1:
            choices = '\n'.join(c['path'] for c in candidates) or 'No se encontraron scripts junto al tablero.'
            raise ValueError('Para elegir el script usa --connect-script RUTA.\n' + choices)
        script = candidates[0]['path']
    path = Path(script).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in backend.SCRIPT_TYPES:
        raise ValueError('Selecciona un archivo .ps1, .sh, .bat o .cmd existente.')
    env = dict(os.environ, EKS_CONSOLE_SCRIPT=str(path), EKS_CONSOLE_PYTHON=sys.executable,
               EKS_CONSOLE_APP=str(Path(__file__).resolve()), EKS_CONSOLE_PORT=str(args.port))
    extra = ' --no-browser' if args.no_browser else ''
    if path.suffix.lower() == '.ps1':
        shell = shutil.which('powershell') or shutil.which('pwsh')
        if not shell: raise ValueError('PowerShell no está disponible.')
        command = [shell, '-NoProfile', '-Command',
            "$ErrorActionPreference='Stop'; . $env:EKS_CONSOLE_SCRIPT; if (-not $?) { exit 1 }; "
            "& $env:EKS_CONSOLE_PYTHON $env:EKS_CONSOLE_APP --port $env:EKS_CONSOLE_PORT "
            "--connected-script $env:EKS_CONSOLE_SCRIPT" + extra + '; exit $LASTEXITCODE']
    elif path.suffix.lower() == '.sh':
        shell = shutil.which('bash')
        if not shell: raise ValueError('Bash no está disponible.')
        command = [shell, '-c', 'source "$EKS_CONSOLE_SCRIPT" && "$EKS_CONSOLE_PYTHON" "$EKS_CONSOLE_APP" '
            '--port "$EKS_CONSOLE_PORT" --connected-script "$EKS_CONSOLE_SCRIPT"' + extra]
    else:
        if os.name != 'nt': raise ValueError('Los scripts BAT/CMD requieren Windows.')
        if any(ch in str(path) + sys.executable + str(backend.BASE) for ch in '%!^&|<>\r\n"'):
            raise ValueError('Para BAT/CMD usa rutas sin caracteres especiales de la consola.')
        command = ['cmd.exe', '/d', '/v:off', '/s', '/c',
            'call "%EKS_CONSOLE_SCRIPT%" && "%EKS_CONSOLE_PYTHON%" "%EKS_CONSOLE_APP%" '
            '--port "%EKS_CONSOLE_PORT%" --connected-script "%EKS_CONSOLE_SCRIPT%"' + extra]
    print(f'Conectando mediante {path.name}. Completa el inicio de sesión en esta terminal.', flush=True)
    return subprocess.call(command, env=env, cwd=str(path.parent))


def main():
    parser = argparse.ArgumentParser(description='Bancolombia | EKS Console local. Python + kubectl.')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--demo', action='store_true', help='Mostrar datos simulados para revisar el diseño')
    parser.add_argument('--connect-script', help='Ejecutar tu script y heredar su sesión; RUTA o auto')
    parser.add_argument('--connected-script', default='', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.connect_script and args.demo: parser.error('Usa --demo o --connect-script por separado.')
    existing = existing_instance_url(args.port)
    if existing:
        return reopen_existing(existing, args.no_browser)
    if args.connect_script:
        try: return connect_and_launch(args.connect_script, args)
        except (ValueError, OSError) as exc: parser.error(str(exc))
    backend.DEMO, backend.CONNECTED_SCRIPT = args.demo, args.connected_script
    try: httpd = ThreadingHTTPServer(('127.0.0.1', args.port), server.Handler)
    except OSError as exc:
        existing = existing_instance_url(args.port, attempts=5)
        if existing:
            return reopen_existing(existing, args.no_browser)
        parser.error(f'No se puede abrir el puerto {args.port}: {exc}')
    backend.PERF=backend.PerformanceMonitor()
    url = f'http://127.0.0.1:{httpd.server_port}'
    print(f'Bancolombia | EKS Console\n{url}\n' + ('DEMO: datos simulados.\n' if backend.DEMO else '') + 'Ctrl+C para cerrar.', flush=True)
    if not args.no_browser:
        timer = threading.Timer(.15, lambda: webbrowser.open(url)); timer.daemon = True; timer.start()
    try: httpd.serve_forever()
    except KeyboardInterrupt: print('\nTablero detenido.')
    finally:
        backend.PERF.close();backend.AZURE.disconnect();backend.AWS_SESSIONS.clear();httpd.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
