"""Credenciales temporales y verificación de sesiones AWS para EKS Console."""

import json
import os
import re
import shutil
import threading
import time


AWS_KEYS = {
    'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
    'AWS_REGION', 'AWS_DEFAULT_REGION',
}


def parse_environment_block(text, allowed):
    """Interpreta asignaciones de variables sin ejecutar el texto pegado."""
    if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
        raise ValueError('Pega un bloque de variables válido, de hasta 100 KB.')
    values = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#') or re.match(r'(?i)^rem\s', line):
            continue
        for statement in line.split(';'):
            statement = statement.strip()
            if not statement:
                continue
            if re.match(r'(?i)^set\s+"', statement) and statement.endswith('"'):
                statement = re.sub(r'(?i)^set\s+"', '', statement)[:-1]
            match = re.fullmatch(
                r'(?i)(?:(?:export|set)\s+|\$env:)?([A-Z_][A-Z_0-9]*)\s*=\s*(.*)',
                statement,
            )
            if not match or match.group(1).upper() not in allowed:
                raise ValueError(
                    f'Línea {line_number}: se admiten asignaciones de variables de credenciales. '
                    'Para un script con comandos usa la conexión por archivo.'
                )
            key, value = match.group(1).upper(), match.group(2).strip()
            if value[:1] in ('"', "'"):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError(f'Línea {line_number}: comillas sin cerrar.')
                value = value[1:-1]
            if not value or '\x00' in value or len(value) > 20_000:
                raise ValueError(f'Línea {line_number}: valor vacío o no válido.')
            if any(symbol in value for symbol in ('$(', '`', '${')):
                raise ValueError(
                    f'Línea {line_number}: pega el valor literal; no una expresión de la terminal.'
                )
            if key in values and values[key] != value:
                raise ValueError(
                    f'Línea {line_number}: la misma variable aparece con dos valores distintos.'
                )
            values[key] = value
    return values


class AwsSessionManager:
    """Mantiene credenciales solo en memoria y comprueba su acceso."""

    def __init__(self):
        self.sessions = {}
        self.lock = threading.RLock()

    def process_env(self, context):
        with self.lock:
            session = self.sessions.get(context)
        if not session:
            return None
        env = dict(os.environ)
        for key in (
            'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
            'AWS_SECURITY_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE',
            'AWS_ROLE_ARN', 'AWS_WEB_IDENTITY_TOKEN_FILE',
        ):
            env.pop(key, None)
        env.update(session['values'])
        return env

    def status(self, context, *, demo, resolve_context, kubectl, run_command):
        context = str(context or '').strip()
        if demo:
            return {
                'connected': True, 'state': 'connected', 'source': 'demo',
                'context': context or 'demo-eks-qa', 'account': '000000000000',
                'arn': 'arn:aws:sts::000000000000:assumed-role/demo/eks-console',
                'message': 'Sesión de demostración activa. No se consultó AWS.',
            }
        if not context:
            return {
                'connected': False, 'state': 'no_context', 'source': 'none', 'context': '',
                'message': 'Selecciona un contexto de Kubernetes para verificar la conexión.',
            }
        try:
            context = resolve_context(context)
        except (RuntimeError, ValueError) as exc:
            return {
                'connected': False, 'state': 'no_context', 'source': 'none',
                'context': context, 'message': str(exc),
            }
        with self.lock:
            temporary = context in self.sessions
            loaded_at = self.sessions.get(context, {}).get('loaded_at')
        if temporary:
            if not shutil.which('aws'):
                return {
                    'connected': False, 'state': 'missing_aws_cli', 'source': 'temporary',
                    'context': context, 'message': 'AWS CLI no está disponible en el PATH.',
                }
            env = self.process_env(context) or dict(os.environ)
            env['AWS_PAGER'] = ''
            try:
                identity = json.loads(run_command(
                    ['aws', 'sts', 'get-caller-identity', '--output', 'json', '--no-cli-pager'],
                    timeout=6,
                    env=env,
                ))
                return {
                    'connected': True, 'state': 'connected', 'source': 'temporary',
                    'context': context, 'account': identity.get('Account', ''),
                    'arn': identity.get('Arn', ''), 'loaded_at': loaded_at,
                    'message': 'Credenciales temporales verificadas por AWS.',
                }
            except (RuntimeError, ValueError) as exc:
                return {
                    'connected': False, 'state': 'invalid_credentials', 'source': 'temporary',
                    'context': context, 'message': str(exc),
                }
        if not shutil.which('kubectl'):
            return {
                'connected': False, 'state': 'missing_kubectl', 'source': 'cli',
                'context': context, 'message': 'kubectl no está disponible en el PATH.',
            }
        try:
            kubectl('--request-timeout=5s', 'version', '-o', 'json', context=context, timeout=7)
            profile = os.environ.get('AWS_PROFILE') or os.environ.get('AWS_DEFAULT_PROFILE') or ''
            return {
                'connected': True, 'state': 'connected', 'source': 'cli', 'context': context,
                'profile': profile, 'message': 'La sesión local puede autenticarse contra el clúster.',
            }
        except RuntimeError as exc:
            return {
                'connected': False, 'state': 'login_required', 'source': 'cli',
                'context': context, 'message': str(exc),
            }

    def connect(self, data, *, demo, resolve_context, kubectl, run_command):
        values = parse_environment_block(data.get('block', ''), AWS_KEYS)
        required = {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'}
        if not required.issubset(values):
            raise ValueError(
                'El bloque temporal debe incluir AWS_ACCESS_KEY_ID, '
                'AWS_SECRET_ACCESS_KEY y AWS_SESSION_TOKEN.'
            )
        context = str(data.get('context', '')).strip()
        if not context:
            raise ValueError('Selecciona primero el contexto EKS al que corresponde el bloque.')
        if demo:
            return {
                'loaded': True, 'context': context, 'demo': True,
                'status': self.status(
                    context, demo=True, resolve_context=resolve_context,
                    kubectl=kubectl, run_command=run_command,
                ),
            }
        context = resolve_context(context)
        with self.lock:
            self.sessions[context] = {'values': values, 'loaded_at': time.time()}
        status = self.status(
            context, demo=False, resolve_context=resolve_context,
            kubectl=kubectl, run_command=run_command,
        )
        if not status.get('connected'):
            with self.lock:
                self.sessions.pop(context, None)
            raise ValueError(
                'AWS rechazó las credenciales temporales: '
                + status.get('message', 'verifica el bloque pegado.')
            )
        return {'loaded': True, 'context': context, 'demo': False, 'status': status}
