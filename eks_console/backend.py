#!/usr/bin/env python3
"""Bancolombia | EKS Console, propuesta local de solo lectura.

Python 3.9+ y kubectl. Performance, Azure DevOps y ambientes restringidos. Sin pip, CDN ni recursos externos.
  py pods_local.py
  py pods_local.py --demo
  py pods_local.py --connect-script auto
  py pods_local.py --connect-script C:\\ruta\\conectar.ps1

Detecta scripts junto al archivo o en ./scripts. Nunca ejecuta un candidato
solo por encontrarlo: --connect-script solicita expresamente su ejecución.
Los scripts PS1/SH/BAT se ejecutan en terminal y lanzan Python como hijo para
heredar AWS_PROFILE, KUBECONFIG y credenciales temporales. Se respeta la política
PowerShell del equipo. La compatibilidad con cada script debe validarse.
"""
import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BASE = Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
SCRIPT_TYPES = {'.ps1', '.sh', '.bat', '.cmd'}
DEMO = False
CONNECTED_SCRIPT = ''


def run_command(command, timeout=15, env=None):
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=timeout, check=False, env=env,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0)
    except FileNotFoundError as exc:
        raise RuntimeError(f'No se encuentra {command[0]}. Revisa el PATH de esta terminal.') from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('La consulta superó el tiempo de espera. Revisa VPN y sesión de AWS.') from exc
    if completed.returncode:
        message=(completed.stderr or completed.stdout or 'Consulta fallida').strip()
        for key in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN'):
            secret=(env or {}).get(key)
            if secret:message=message.replace(secret,'[oculto]')
        raise RuntimeError(message[:900])
    if len(completed.stdout) > 12_000_000:
        raise RuntimeError('Respuesta demasiado grande. Consulta un namespace específico.')
    return completed.stdout


def kubectl(*args, context=None, timeout=15):
    command = ['kubectl']
    if context:
        command += ['--context', context]
    return run_command(command + list(args), timeout, env=aws_process_env(context))


def scope(namespace, allow_all=True):
    if namespace == '*' and allow_all:
        return ['-A']
    if not NAME.fullmatch(namespace) or '.' in namespace or len(namespace) > 63:
        raise ValueError('Escribe un namespace válido; usa * para todos si tienes permiso.')
    return ['-n', namespace]


def validate_name(value):
    if not NAME.fullmatch(value) or len(value) > 253:
        raise ValueError('Nombre de recurso inválido.')
    return value


def discover_scripts():
    roots = {BASE, Path.cwd().resolve(), BASE / 'scripts', Path.cwd().resolve() / 'scripts'}
    results = []
    seen = set()
    for root in sorted(roots, key=str):
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())[:300]
        except OSError:
            continue
        for path in entries:
            try:
                if path.suffix.lower() not in SCRIPT_TYPES or not path.is_file() or path.stat().st_size > 512_000:
                    continue
                path = path.resolve()
                if str(path) in seen:
                    continue
                seen.add(str(path))
                content = path.read_text(encoding='utf-8-sig', errors='replace').lower()
                hints = [label for pattern, label in [
                    ('update-kubeconfig', 'Configura EKS'), ('aws sso login', 'Inicio SSO'),
                    ('saml2aws', 'Autenticación SAML'), ('aws-vault', 'AWS Vault'),
                    ('aws_profile', 'Perfil AWS'), ('aws_access_key_id', 'Sesión AWS'),
                    ('assume-role', 'Asume un rol'), ('get-session-token', 'Sesión temporal'),
                ] if pattern in content]
                if hints:
                    results.append({'name': path.name, 'path': str(path), 'hints': hints})
            except OSError:
                continue
    return results[:30]


def profile_names():
    # Solo se lee el archivo de configuración; nunca el de credenciales.
    path = Path(os.environ.get('AWS_CONFIG_FILE', str(Path.home() / '.aws' / 'config')))
    parser = configparser.RawConfigParser()
    try:
        parser.read(path, encoding='utf-8')
        return [s[8:] if s.startswith('profile ') else s for s in parser.sections()
                if s == 'default' or s.startswith('profile ')]
    except (OSError, configparser.Error):
        return []


def config_info():
    if DEMO:
        return {'contexts': [{'name': 'demo-eks-qa', 'label': 'eks-documentos-qa', 'namespace': 'generaciondocumentaldigital-qa', 'eks': True, 'region': 'us-east-1'}],
                'current': 'demo-eks-qa', 'profiles': ['demo-qa'], 'profile': 'demo-qa',
                'scripts': [], 'connected_script': '', 'demo': True, 'error': None,
                'kubectl': True, 'aws': True, 'temporary_contexts': []}
    info = {'contexts': [], 'current': '', 'profiles': profile_names(),
            'profile': os.environ.get('AWS_PROFILE') or os.environ.get('AWS_DEFAULT_PROFILE') or '',
            'scripts': discover_scripts(), 'connected_script': Path(CONNECTED_SCRIPT).name if CONNECTED_SCRIPT else '',
            'demo': False, 'kubectl': bool(shutil.which('kubectl')), 'aws': bool(shutil.which('aws')), 'error': None}
    with AWS_SESSION_LOCK:
        info['temporary_contexts'] = sorted(AWS_SESSIONS)
    try:
        cfg = json.loads(kubectl('config', 'view', '-o', 'json', timeout=6))
        info['current'] = cfg.get('current-context', '')
        for c in cfg.get('contexts') or []:
            context = c.get('context') or {}
            name = c.get('name', '')
            cluster = context.get('cluster', '')
            match = re.search(r'arn:aws[^:]*:eks:([^:]+):[^:]+:cluster/(.+)', cluster)
            info['contexts'].append({'name': name, 'label': name.split('/')[-1],
                'namespace': context.get('namespace') or 'default', 'eks': bool(match),
                'region': match.group(1) if match else ''})
    except (RuntimeError, ValueError) as exc:
        info['error'] = str(exc)
    return info


def resolve_context(context):
    # Se fija un contexto por consulta, sin modificar el contexto global.
    cfg = json.loads(kubectl('config', 'view', '-o', 'json', timeout=6))
    selected = context or cfg.get('current-context', '')
    if not selected or selected not in {c.get('name') for c in cfg.get('contexts') or []}:
        raise ValueError('No hay un contexto válido. Ejecuta tu conexión a EKS y vuelve a detectar.')
    return selected


def number(quantity, cpu=False):
    if quantity is None:
        return None
    match = re.fullmatch(r'([0-9.]+)([a-zA-Z]*)', str(quantity))
    if not match:
        return None
    value, unit = float(match.group(1)), match.group(2)
    if cpu:
        factor = {'': 1000, 'm': 1, 'u': .001, 'n': .000001}.get(unit)
        return value * factor if factor is not None else None
    multiplier = {'': 1, 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4,
                  'K': 1000, 'k': 1000, 'm': .001, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4}.get(unit)
    return value * multiplier / 1024**2 if multiplier else None


def age(iso):
    if not iso:return '—'
    try:
        elapsed = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace('Z', '+00:00'))).total_seconds()))
        if elapsed < 60: return f'{elapsed}s'
        if elapsed < 3600: return f'{elapsed // 60}m'
        if elapsed < 86400: return f'{elapsed // 3600}h'
        return f'{elapsed // 86400}d'
    except (TypeError, ValueError):
        return '—'


def pod_state(item):
    meta, status, spec = item.get('metadata') or {}, item.get('status') or {}, item.get('spec') or {}
    phase = status.get('phase') or 'Unknown'
    if meta.get('deletionTimestamp'): return 'Terminating', 'warning', False
    ready = any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions') or [])
    init_spec = {c['name']: c for c in spec.get('initContainers') or []}
    for c in status.get('initContainerStatuses') or []:
        state = c.get('state') or {}
        if state.get('waiting', {}).get('reason'):
            return 'Init:' + state['waiting']['reason'], 'danger', False
        if state.get('terminated', {}).get('exitCode', 0) != 0:
            return 'Init:' + state['terminated'].get('reason', 'Error'), 'danger', False
        if 'running' in state and init_spec.get(c.get('name'), {}).get('restartPolicy') != 'Always':
            return 'Initializing', 'warning', False
    if phase == 'Succeeded': return 'Completed', 'neutral', False
    if phase == 'Failed': return status.get('reason') or 'Failed', 'danger', False
    waiting = [(c.get('state') or {}).get('waiting') or {} for c in status.get('containerStatuses') or []]
    priority = ['CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError', 'CreateContainerError']
    reasons = [w.get('reason') for w in waiting if w.get('reason')]
    for reason in priority:
        if reason in reasons: return reason, 'danger', False
    if reasons: return reasons[0], 'warning', False
    terminated = [(c.get('state') or {}).get('terminated') or {} for c in status.get('containerStatuses') or []]
    for state in terminated:
        if state.get('exitCode', 0) != 0: return state.get('reason', 'Error'), 'danger', False
    if phase == 'Running': return ('Running', 'good', True) if ready else ('NotReady', 'warning', False)
    return phase, 'warning', False


def _base_pod_record(item, metrics):
    meta, status, spec = item.get('metadata') or {}, item.get('status') or {}, item.get('spec') or {}
    containers, stats = spec.get('containers') or [], status.get('containerStatuses') or []
    state, severity, ready = pod_state(item)
    labels = meta.get('labels') or {}
    owners = meta.get('ownerReferences') or []
    owner = next((o for o in owners if o.get('controller')), owners[0] if owners else {})
    micro = labels.get('app.kubernetes.io/name') or labels.get('app') or owner.get('name') or meta.get('name', '')
    if not labels.get('app.kubernetes.io/name') and not labels.get('app') and owner.get('kind') == 'ReplicaSet':
        micro = micro.rsplit('-', 1)[0]
    cpu, memory = metrics.get('cpu'), metrics.get('memory')
    limits = [c.get('resources', {}).get('limits', {}) for c in containers]
    cpu_limit = sum(number(r['cpu'], True) or 0 for r in limits) if limits and all('cpu' in r for r in limits) else None
    mem_limit = sum(number(r['memory']) or 0 for r in limits) if limits and all('memory' in r for r in limits) else None
    details = []
    by_name = {c.get('name'): c for c in stats + (status.get('initContainerStatuses') or [])}
    for c in containers + (spec.get('initContainers') or []):
        cs = by_name.get(c['name'], {})
        details.append({'name': c['name'], 'image': c.get('image', ''), 'ready': bool(cs.get('ready')),
                        'restarts': cs.get('restartCount', 0), 'last_reason': cs.get('lastState', {}).get('terminated', {}).get('reason', ''),
                        'resources': c.get('resources') or {}})
    return {'name': meta.get('name', ''), 'namespace': meta.get('namespace', ''), 'uid': meta.get('uid', ''),
            'micro': micro, 'state': state, 'severity': severity, 'is_ready': ready,
            'ready': f"{sum(bool(c.get('ready')) for c in stats)}/{len(containers)}",
            'restarts': sum(c.get('restartCount', 0) for c in stats + (status.get('initContainerStatuses') or [])),
            'cpu': cpu, 'memory': memory, 'cpu_limit': cpu_limit, 'memory_limit': mem_limit,
            'node': spec.get('nodeName') or 'Sin asignar', 'ip': status.get('podIP') or '—',
            'age': age(meta.get('creationTimestamp')), 'created': meta.get('creationTimestamp', ''),
            'conditions': status.get('conditions') or [], 'containers': details}


def metrics_for(namespace, context):
    try:
        raw = kubectl('top', 'pods', *scope(namespace), '--no-headers', context=context, timeout=10)
        data = {}
        for line in raw.splitlines():
            parts = line.split()
            if namespace == '*' and len(parts) >= 4: ns, name, cpu, mem = parts[:4]
            elif namespace != '*' and len(parts) >= 3: ns, (name, cpu, mem) = namespace, parts[:3]
            else: continue
            data[(ns, name)] = {'cpu': number(cpu, True), 'memory': number(mem)}
        return data, None
    except RuntimeError as exc:
        return {}, str(exc)


def list_pods(namespace, context):
    scope(namespace)
    if DEMO: return demo_snapshot(namespace, context)
    context = resolve_context(context)
    with ThreadPoolExecutor(max_workers=2) as pool:
        metrics_task = pool.submit(metrics_for, namespace, context)
        raw = json.loads(kubectl('get', 'pods', *scope(namespace), '-o', 'json', context=context))
        metrics, error = metrics_task.result()
    pods = [pod_record(p, metrics.get((p['metadata'].get('namespace'), p['metadata'].get('name')), {}))
            for p in raw.get('items') or []]
    pods.sort(key=lambda p: ({'danger': 0, 'warning': 1, 'good': 2, 'neutral': 3}[p['severity']], p['name']))
    return {'pods': pods, 'context': context, 'namespace': namespace, 'metrics_error': error,
            'updated': datetime.now().astimezone().isoformat(timespec='seconds'), 'demo': False}


def _base_demo_snapshot(namespace, context):
    pods = []
    for i in range(9):
        micro = ['generador', 'renderizador', 'orquestador'][i // 3]
        state, severity, ready = ('Running', 'good', True)
        if i == 3: state, severity, ready = ('CrashLoopBackOff', 'danger', False)
        if i == 8: state, severity, ready = ('Pending', 'warning', False)
        pods.append({'name': f'{micro}-7dc84c9f6-{["z7k2p", "m4r8w", "b9v3n"][i % 3]}',
          'namespace': 'generaciondocumentaldigital-qa', 'uid': f'demo-{i}', 'micro': micro,
          'state': state, 'severity': severity, 'is_ready': ready, 'ready': '1/1' if ready else '0/1',
          'restarts': 6 if i == 3 else (1 if i == 1 else 0), 'cpu': None if i == 8 else [340, 275, 410, 25, 580, 420, 125, 98][i],
          'memory': None if i == 8 else [680, 590, 710, 130, 850, 780, 245, 225][i],
          'cpu_limit': 1000, 'memory_limit': 2048, 'node': f'ip-10-0-{i % 3 + 1}-24.ec2.internal' if i != 8 else 'Sin asignar',
          'ip': f'10.4.1.{10+i}' if i != 8 else '—', 'age': '2d' if i != 8 else '1m',
          'created': '2026-09-23T14:00:00Z', 'conditions': [],
          'containers': [{'name': micro, 'image': f'demo/{micro}:1.4.2', 'ready': ready,
                          'restarts': 6 if i == 3 else 0, 'last_reason': 'OOMKilled' if i == 3 else '',
                          'resources': {'requests': {'cpu':'250m','memory':'512Mi'}, 'limits': {'cpu':'1','memory':'2Gi'}}}]})
    if namespace not in ('*', 'generaciondocumentaldigital-qa'): pods = []
    pods.sort(key=lambda p: ({'danger':0, 'warning':1, 'good':2}[p['severity']], p['name']))
    return {'pods': pods, 'context': context or 'demo-eks-qa', 'namespace': namespace, 'metrics_error': None,
            'updated': datetime.now().astimezone().isoformat(timespec='seconds'), 'demo': True}


def pod_content(kind, namespace, pod, context, container='', previous=False, since=''):
    scope(namespace, False); validate_name(pod)
    if container: validate_name(container)
    if since and (len(since) > 64 or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})', since)):
        raise ValueError('Marca de tiempo de logs no válida.')
    if DEMO:
        if kind == 'logs':
            now = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            if since:
                return {'text': f'{now} INFO  [SIMULADO] Captura activa · verificación periódica correcta'}
            return {'text': '[DATOS SIMULADOS]\n2026-09-25T14:29:58.000Z INFO  Servicio iniciado\n2026-09-25T14:29:59.000Z INFO  Documento procesado · 284 ms\n2026-09-25T14:30:00.000Z WARN  Ejemplo de diagnóstico; no corresponde al banco.'}
        return {'events': [{'type':'Warning', 'reason':'BackOff', 'message':'[Simulado] Back-off restarting failed container', 'count':6, 'time':'2026-09-25T14:30:00Z'}]}
    context = resolve_context(context)
    if kind == 'logs':
        args = ['logs', '-n', namespace, pod, '--limit-bytes=180000', '--timestamps=true']
        args += [f'--since-time={since}'] if since else ['--tail=200']
        if container: args += ['-c', container]
        if previous: args += ['--previous=true']
        return {'text': kubectl(*args, context=context)}
    # UID evita mezclar eventos de otra encarnación del mismo nombre de pod.
    current = json.loads(kubectl('get', 'pod', pod, '-n', namespace, '-o', 'json', context=context))
    uid = current.get('metadata', {}).get('uid', '')
    selector = f'involvedObject.uid={uid}' if uid else f'involvedObject.name={pod},involvedObject.kind=Pod'
    raw = json.loads(kubectl('get', 'events', '-n', namespace, '--field-selector', selector, '-o', 'json', context=context))
    events = [{'type': e.get('type', ''), 'reason': e.get('reason', ''), 'message': e.get('message', ''),
               'count': e.get('count') or e.get('series', {}).get('count') or 1,
               'time': e.get('lastTimestamp') or e.get('eventTime') or e.get('metadata', {}).get('creationTimestamp', '')}
              for e in raw.get('items') or []]
    return {'events': sorted(events, key=lambda e: e['time'], reverse=True)[:60]}


# HTML se inserta aquí al generar el archivo único.

# Performance y Azure DevOps: consultas remotas de lectura; token solo en memoria.
import base64
import csv
import io
import math
import secrets
import ssl
import time
import urllib.request
import urllib.error
from collections import deque, defaultdict
from urllib.parse import quote, urlencode

LOCAL_TOKEN = secrets.token_urlsafe(32)
PERF = None


def resource_access(resource, namespace, context):
    try:
        data = json.loads(kubectl('get', resource, *scope(namespace), '-o', 'json', context=context, timeout=12))
        return {'status': 'available', 'items': data.get('items') or []}
    except RuntimeError as exc:
        message = str(exc)
        low = message.lower()
        return {'status': 'forbidden' if 'forbidden' in low or 'cannot list' in low else 'unavailable',
                'items': None, 'message': message}


def hpa_record(item):
    meta, spec, status = item.get('metadata', {}), item.get('spec', {}), item.get('status', {})
    return {'name': meta.get('name'), 'namespace': meta.get('namespace'),
            'target': spec.get('scaleTargetRef', {}), 'min': spec.get('minReplicas', 1),
            'max': spec.get('maxReplicas'), 'current': status.get('currentReplicas'),
            'desired': status.get('desiredReplicas'), 'targets': spec.get('metrics') or [],
            'metrics': status.get('currentMetrics') or [], 'conditions': status.get('conditions') or []}


def capacity_snapshot(namespace, context, micro=''):
    data = list_pods(namespace, context)
    pods = [p for p in data['pods'] if not micro or p['micro'] == micro]
    if len(pods)>500: raise ValueError('Performance admite hasta 500 pods por ámbito. Selecciona un namespace o microservicio más específico.')
    data['pods'] = pods
    if DEMO:
        resources = {'hpa': {'status':'available','items':[]}, 'deployments':{'status':'available','items':[]}}
        hpas = [{'name':m, 'namespace':namespace, 'target':{'kind':'Deployment','name':m},
                 'min':3,'max':12,'current':3,'desired':3,'targets':[{'type':'Resource','resource':{'name':'cpu','target':{'type':'Utilization','averageUtilization':65}}}],
                 'metrics':[{'type':'Resource','resource':{'name':'cpu','current':{'averageUtilization':58}}}], 'conditions':[]}
                for m in sorted({p['micro'] for p in pods})]
        deployments = [{'name':m,'desired':3,'ready':sum(p['is_ready'] for p in pods if p['micro']==m),'available':sum(p['is_ready'] for p in pods if p['micro']==m)} for m in sorted({p['micro'] for p in pods})]
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = {r:pool.submit(resource_access,r,namespace,data['context']) for r in ['hpa','deployments']}
            resources = {r:t.result() for r,t in tasks.items()}
        hpas = [hpa_record(h) for h in resources['hpa']['items'] or []]
        deployments = [{'name':p.get('metadata',{}).get('name'), 'desired':p.get('spec',{}).get('replicas',1),
                        'ready':p.get('status',{}).get('readyReplicas',0),'available':p.get('status',{}).get('availableReplicas',0)}
                       for p in resources['deployments']['items'] or []]
        if micro:
            # La etiqueta app puede diferir del nombre del Deployment: conservar la lista
            # e indicar el ámbito de HPA para no atribuir silenciosamente otro workload.
            hpas = [h for h in hpas if h['target'].get('name') == micro]
            deployments = [d for d in deployments if d['name'] == micro]
    current_pods = [p for p in pods if p['state'] not in ('Completed', 'Succeeded', 'Terminating')]
    sample = {'time': time.time(), 'total':len(current_pods), 'ready':sum(p['is_ready'] for p in current_pods),
              'attention':sum(p['severity'] in ('danger','warning') for p in current_pods),
              'restarts':sum(p['restarts'] for p in current_pods),
              'restarts_by_uid':{p.get('uid') or p['name']:p['restarts'] for p in current_pods},
              'measured':sum(p['cpu'] is not None and p['memory'] is not None for p in current_pods)}
    for key in ['cpu','memory','cpu_request','memory_request','cpu_limit','memory_limit']:
        values = [p.get(key) for p in current_pods]
        sample[key] = (sum(v for v in values if v is not None) if any(v is not None for v in values) else None) if key in ('cpu','memory') else (sum(values) if values and all(v is not None for v in values) else None)
        sample[key+'_coverage'] = sum(v is not None for v in values)
    data.update(sample=sample,hpas=hpas,deployments=deployments,
                access={r:{k:v for k,v in a.items() if k!='items'} for r,a in resources.items()})
    return data


def inventory(namespace, context):
    scope(namespace)
    kinds=['deployments','statefulsets','daemonsets','hpa','services','ingresses','jobs','cronjobs','pvc','resourcequotas','limitranges','replicasets','poddisruptionbudgets','networkpolicies']
    if DEMO:
        return {'resources':[{'kind':r,'status':'available','count':3 if r in ('deployments','hpa','services') else 0,
                             'names':['generador','renderizador','orquestador'] if r in ('deployments','hpa','services') else []}
                            for r in kinds], 'demo':True}
    context=resolve_context(context)
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks={r:pool.submit(resource_access,r,namespace,context) for r in kinds}
        results=[]
        for kind,task in tasks.items():
            value=task.result();items=value.pop('items')
            value.update(kind=kind,count=len(items) if items is not None else None,
                         names=[p.get('metadata',{}).get('name') for p in items or []][:100])
            if kind in ('resourcequotas','limitranges'):
                value['detail']=[{'name':p.get('metadata',{}).get('name'),'spec':p.get('spec'),
                                  'status':p.get('status')} for p in items or []]
            results.append(value)
    try:
        visible=json.loads(kubectl('get','pods',*scope(namespace),'-o','json',context=context))
        node_names=sorted({p.get('spec',{}).get('nodeName') for p in visible.get('items',[]) if p.get('spec',{}).get('nodeName')})[:50]
        if node_names:
            nodes=json.loads(kubectl('get','nodes',*node_names,'-o','json',context=context))
            items=nodes.get('items') or ([nodes] if nodes.get('kind')=='Node' else [])
            results.append({'kind':'nodes (de los pods visibles)','status':'available','count':len(items),'names':node_names,
                'detail':[{'name':n.get('metadata',{}).get('name'),'allocatable':n.get('status',{}).get('allocatable'),
                           'conditions':[c for c in n.get('status',{}).get('conditions',[]) if c.get('type')=='Ready']} for n in items]})
    except RuntimeError as exc:
        results.append({'kind':'nodes (de los pods visibles)','status':'forbidden' if 'forbidden' in str(exc).lower() else 'unavailable','count':None,'message':str(exc),'names':[]})
    return {'resources':results,'context':context,'namespace':namespace,'demo':False}


from .azure import AzureClient, safe_variables
AZURE=AzureClient(run_command)


def percentile(values,p):
    return sorted(values)[max(0,math.ceil(len(values)*p)-1)] if values else None


def parse_jtl(text,label_filter=''):
    if len(text)>10_000_000: raise ValueError('El archivo supera 10 MB. Exporta la prueba o un intervalo más pequeño.')
    reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not {'timeStamp','elapsed','success'}.issubset(set(reader.fieldnames or [])):
        raise ValueError('Importa JTL/CSV de muestras con encabezados timeStamp, elapsed y success; no un resumen agregado.')
    rows=[];labels=set()
    for row in reader:
        label=row.get('label','');labels.add(label)
        if label_filter and label!=label_filter: continue
        if len(rows)>=200_000: raise ValueError('Máximo 200.000 muestras por archivo.')
        try:
            timestamp=float(row['timeStamp']);elapsed=float(row['elapsed'])
            if row['success'].lower() not in ('true','false'): raise ValueError('success')
            success=row['success'].lower()=='true'
            if not math.isfinite(timestamp) or not math.isfinite(elapsed) or timestamp<=0 or elapsed<0: raise ValueError()
            if float(row.get('SampleCount') or row.get('sampleCount') or 1)!=1: raise ValueError('aggregated')
        except (TypeError,ValueError): raise ValueError('Hay muestras inválidas o agregadas; usa muestras individuales de JMeter.') from None
        rows.append((timestamp,elapsed,success,label))
    if not rows: raise ValueError('No hay muestras para el filtro indicado.')
    durations=[r[1] for r in rows];start=min(r[0] for r in rows);end=max(r[0]+r[1] for r in rows);seconds=(end-start)/1000
    buckets=defaultdict(list);bucket_size=max(1000,math.ceil((end-start)/100)*1.0)
    for r in rows: buckets[int((r[0]-start)//bucket_size)].append(r)
    series=[{'time':(start+k*bucket_size)/1000,'p95':percentile([r[1] for r in b],.95),
             'samples':len(b),'errors':sum(not r[2] for r in b)} for k,b in sorted(buckets.items())]
    return {'samples':len(rows),'errors':sum(not r[2] for r in rows),'error_percent':100*sum(not r[2] for r in rows)/len(rows),
            'p50':percentile(durations,.50),'p95':percentile(durations,.95),'p99':percentile(durations,.99),
            'max':max(durations),'average':sum(durations)/len(durations),'throughput':len(rows)/seconds if seconds else None,
            'duration_s':seconds,'start':start/1000,'end':end/1000,'labels':sorted(labels)[:200],
            'label_filter':label_filter,'series':series,
            'note':'Cada fila cuenta como una muestra, no necesariamente un documento. Se asume timeStamp al inicio (valor predeterminado de JMeter). Si hay muestras padre de Transaction Controller y sus hijas, filtra una etiqueta para evitar doble conteo.'}


def analyze(samples,latest,jtl=None,slo=None):
    findings=[];slo=slo or {'p95_ms':1000,'errors_percent':1}
    def add(level,title,evidence,action): findings.append({'level':level,'title':title,'evidence':evidence,'action':action})
    if not samples:
        add('info','Faltan muestras','Aún no hay mediciones de esta captura.','Inicia una captura manual o arma una ejecución de Azure DevOps.')
    else:
        duration=samples[-1]['time']-samples[0]['time'];complete=sum(s.get('measured',0)==s.get('total',0) and s.get('total',0)>0 for s in samples)
        gaps=[b['time']-a['time'] for a,b in zip(samples,samples[1:]) if b['time']-a['time']>45]
        if gaps:add('warning','Intervalos sin observación continua',f'{len(gaps)} pausas de más de 45 s entre muestras.','Revisa errores de conexión; las curvas no reconstruyen datos durante esas pausas.')
        if len(samples)<20 or duration<300:
            add('info','Ventana de observación corta',f'{len(samples)} muestras en {duration/60:.1f} min.','Captura el calentamiento, la carga sostenida y la recuperación antes de ajustar recursos.')
        if complete<len(samples): add('warning','Cobertura parcial de métricas',f'{complete}/{len(samples)} muestras tienen CPU y memoria para todos los pods observados.','Revisa acceso a metrics.k8s.io; un dato ausente no representa consumo cero.')
        for key,label in [('cpu','CPU'),('memory','Memoria')]:
            observed=[s[key] for s in samples if s.get(key) is not None]
            ratios=[s[key]/s[key+'_limit']*100 for s in samples if s.get(key) is not None and s.get(key+'_limit') and s[key+'_limit']>0]
            if observed:
                unit='mCPU' if key=='cpu' else 'MiB'
                level='warning' if ratios and max(ratios)>=85 else 'info'
                add(level,f'{label}: consumo observado',f'P95 de la suma del ámbito: {percentile(observed,.95):.1f} {unit}; máximo: {max(observed):.1f} {unit}.'+(f' Pico/límite declarado: {max(ratios):.0f}%.' if ratios else ''),'Revisa también cada pod. El promedio agregado puede ocultar un contenedor saturado.')
        increases=sum(s.get('restart_delta',0) for s in samples)
        if increases: add('danger','Reinicios durante la captura',f'Se observaron {increases} incrementos en contadores de los mismos UID.','Correlaciona eventos, última terminación y logs anteriores. Pods que aparecen y desaparecen entre muestras pueden quedar fuera.')
        readiness=[s for s in samples if s.get('ready',0)<s.get('total',0)]
        if readiness: add('warning','Pods sin Ready',f'{len(readiness)}/{len(samples)} muestras incluyeron pods no listos.','Revisa tiempos de arranque, readiness probes, eventos y disponibilidad del Deployment.')
    if latest:
        for p in latest.get('pods',[]):
            for key,label in [('cpu','CPU'),('memory','Memoria')]:
                if p.get(key) is not None and p.get(key+'_limit') and p[key]/p[key+'_limit']>=.85:
                    add('warning',label+' cerca del límite en un pod',p['name']+f': {p[key]/p[key+"_limit"]*100:.0f}% del límite.','Consulta el contenedor y valida contra la carga; esto no prueba por sí solo throttling u OOM.')
        for h in latest.get('hpas',[]):
            if h.get('max') and h.get('current') is not None and h['current']>=h['max']:
                add('warning','HPA en máximo de réplicas',h['name']+f': {h["current"]}/{h["max"]} réplicas.','Comprueba latencia y capacidad del clúster antes de cambiar maxReplicas.')
    if jtl:
        passed=jtl['p95']<=slo['p95_ms'] and jtl['error_percent']<=slo['errors_percent']
        add('good' if passed else 'danger','Resultado frente a tus objetivos',f'P95 {jtl["p95"]:.0f} ms (objetivo ≤ {slo["p95_ms"]:g}); errores {jtl["error_percent"]:.2f}% (objetivo ≤ {slo["errors_percent"]:g}%).','Confirma que el archivo y la etiqueta pertenecen a esta ejecución; este resultado evalúa esas muestras.')
        if samples and (jtl['end']<samples[0]['time'] or jtl['start']>samples[-1]['time']):
            add('warning','JTL fuera del intervalo observado','El archivo no coincide temporalmente con las métricas capturadas.','No atribuyas su latencia al consumo mostrado; selecciona la ejecución correcta.')
    else:
        add('info','Latencia y tasa de errores pendientes','Las métricas de Kubernetes no contienen resultados de peticiones.','Importa el JTL/CSV de esta prueba para calcular percentiles, muestras por segundo y errores.')
    return findings[:30]


class PerformanceMonitor:
    def __init__(self):
        self.lock=threading.RLock();self.stop_event=threading.Event();self.wake=threading.Event();self.generation=0
        self.scope=None;self.mode='idle';self.selection=None;self.latest=None;self.samples=deque(maxlen=1440);self.seen_restarts={}
        self.run=None;self.run_latest=None;self.run_samples=deque(maxlen=1440);self.last_completed_key=None;self.error='';self.azure_error=''
        self.jtl=None;self.slo={'p95_ms':1000,'errors_percent':1};self.started=None;self.ended=None;self.last_azure_poll=0
        self.thread=threading.Thread(target=self.loop,daemon=True);self.thread.start()

    def configure(self,data):
        namespace=str(data.get('namespace','')).strip();scope(namespace)
        context=str(data.get('context','')).strip()
        if not context: raise ValueError('Selecciona el contexto EKS que corresponde al ambiente.')
        mode=data.get('mode','live')
        if mode not in ('live','manual','auto'): raise ValueError('Modo inválido.')
        selection=data.get('selection') or {}
        if mode=='auto':
            if selection.get('kind') not in ('build','release') or not str(selection.get('definition_id','')).isdigit(): raise ValueError('Selecciona un pipeline antes de armar la captura automática.')
            if selection.get('kind')=='release' and not str(selection.get('stage','')).strip(): raise ValueError('Selecciona el ambiente del Release para vincularlo con este ámbito EKS.')
            if not DEMO and not AZURE.summary()['connected']: raise ValueError('Conecta Azure DevOps antes de armar la captura.')
        with self.lock:
            self.generation+=1;self.scope={'namespace':namespace,'context':context,'micro':str(data.get('micro',''))}
            self.mode=mode;self.selection=selection;self.latest=None;self.samples.clear();self.seen_restarts={};self.run_samples.clear();self.run=None;self.run_latest=None
            self.error='';self.azure_error='';self.jtl=None;self.last_completed_key=None;self.started=time.time() if mode=='manual' else None
            self.ended=None;self.last_azure_poll=0
        self.wake.set();return self.status()

    def finish(self):
        with self.lock:
            self.mode='live';self.ended=time.time()
            if self.run:self.run['active']=False;self.run['status']='captura detenida'
        return self.status()

    def import_results(self,data):
        jtl=parse_jtl(str(data.get('csv','')),str(data.get('label','')).strip())
        p95=float(data.get('p95_ms',1000));errors=float(data.get('errors_percent',1))
        if not math.isfinite(p95) or p95<=0 or not math.isfinite(errors) or not 0<=errors<=100: raise ValueError('Objetivos inválidos.')
        with self.lock:self.jtl=jtl;self.slo={'p95_ms':p95,'errors_percent':errors}
        return self.status()

    def status(self):
        with self.lock:
            observed=list(self.run_samples if self.started else self.samples)
            return {'mode':self.mode,'scope':self.scope,'selection':self.selection,'latest':self.latest,
                    'samples':observed,'run':self.run,'started':self.started,'ended':self.ended,'error':self.error,
                    'azure_error':self.azure_error,'jtl':self.jtl,'slo':self.slo,'demo':DEMO,
                    'analysis':analyze(observed,self.run_latest if self.started else self.latest,self.jtl,self.slo),
                    'coverage_note':'Muestras locales cada 15 s, hasta 1.440 (aprox. 6 h). El inicio detectado puede llegar hasta 30 s después de Azure. No se reconstruye historia anterior.'}

    def loop(self):
        while not self.stop_event.is_set():
            self.wake.wait(15);self.wake.clear()
            if self.stop_event.is_set():break
            with self.lock:
                target=dict(self.scope) if self.scope else None;generation=self.generation;mode=self.mode
                selection=dict(self.selection or {});tracked=dict(self.run) if self.run and self.run.get('active') else None
            if not target:continue
            now=time.time()
            if mode=='auto' and now-self.last_azure_poll>=30:
                try:
                    run={'id':2048,'key':'demo:2048','name':'Performance QA · ejemplo','active':True,'status':'inProgress','start':datetime.now(timezone.utc).isoformat()} if DEMO else AZURE.poll(selection,tracked)
                    with self.lock:
                        if generation!=self.generation:continue
                        self.azure_error='';self.last_azure_poll=now
                        if run and run['active'] and run['key']!=self.last_completed_key:
                            if not self.run or self.run['key']!=run['key']:
                                self.run_samples.clear();self.seen_restarts={};self.run_latest=None;self.jtl=None;self.started=now;self.ended=None
                            self.run=run
                        elif self.run and self.run.get('active'):
                            if run is None:
                                self.azure_error='Azure no devolvió la ejecución seguida; se conserva la captura como no confirmada.'
                            else:
                                self.run=run;self.ended=now;self.last_completed_key=run['key']
                except (RuntimeError,ValueError,OSError) as exc:
                    with self.lock:self.azure_error=str(exc);self.last_azure_poll=now
            try:
                current=capacity_snapshot(**target)
                with self.lock:
                    if generation!=self.generation:continue
                    self.latest=current
                    point={k:v for k,v in current['sample'].items() if k!='restarts_by_uid'}
                    counters=current['sample'].get('restarts_by_uid',{})
                    point['restart_delta']=sum(max(0,v-self.seen_restarts.get(k,v)) for k,v in counters.items())
                    self.seen_restarts.update(counters)
                    if len(self.seen_restarts)>10000:self.seen_restarts=dict(counters)
                    self.samples.append(point);self.error=''
                    if self.mode=='manual' or (self.mode=='auto' and self.run and self.run.get('active')):
                        self.run_samples.append(point);self.run_latest=current
            except (RuntimeError,ValueError,OSError) as exc:
                with self.lock:
                    if generation==self.generation:self.error=str(exc)

    def close(self):self.stop_event.set();self.wake.set()

# Las asignaciones se calculan con contenedores residentes. Los init normales y
# el overhead se muestran en detalle, pero no se mezclan con uso de aplicación.
def pod_record(item,metrics):
    record=_base_pod_record(item,metrics)
    spec=item.get('spec') or {}
    resident=(spec.get('containers') or [])+[c for c in spec.get('initContainers') or [] if c.get('restartPolicy')=='Always']
    for resource,key,is_cpu in [('cpu','cpu',True),('memory','memory',False)]:
        for group,suffix in [('requests','request'),('limits','limit')]:
            pod_value=(spec.get('resources') or {}).get(group,{}).get(resource)
            values=[number(c.get('resources',{}).get(group,{}).get(resource),is_cpu) for c in resident]
            record[key+'_'+suffix]=number(pod_value,is_cpu) if pod_value is not None else (sum(values) if values and all(v is not None for v in values) else None)
    init_names={c['name'] for c in spec.get('initContainers') or []}
    for c in record['containers']:c['kind']='Init/sidecar' if c['name'] in init_names else 'Aplicación'
    record['overhead']=spec.get('overhead') or {}
    record['resource_basis']='Pod-level' if spec.get('resources') else 'Contenedores residentes'
    return record

def demo_snapshot(namespace,context):
    data=_base_demo_snapshot(namespace,context)
    wave=1+.12*math.sin(time.time()/25)
    for p in data['pods']:
        p['cpu_request']=250 if p['micro']=='orquestador' else 500
        p['memory_request']=512 if p['micro']=='orquestador' else 1024
        p['resource_basis']='Contenedores residentes';p['overhead']={}
        if p['cpu'] is not None:p['cpu']=round(p['cpu']*wave,2)
        if p['memory'] is not None:p['memory']=round(p['memory']*(1+.025*math.sin(time.time()/40)),2)
    return data


def azure_definitions():
    if DEMO:return {'connected':True,'organization':'demo','project':'Generación documental','auth':'demo',
                   'definitions':[{'id':10,'kind':'build','name':'Performance_GeneracionDocumental_QA'},
                                  {'id':20,'kind':'release','name':'GeneracionDocumental_Release'}],'warnings':[]}
    return AZURE.definitions()


def azure_definition(kind,definition_id):
    if DEMO:return {'id':int(definition_id),'kind':kind,'name':'Performance · demostración',
                   'environments':[{'id':1,'name':'QA'},{'id':2,'name':'PDN'}] if kind=='release' else [],
                   'variables':safe_variables({'CPU_REQUEST':{'value':'500m'},'CPU_LIMIT':{'value':'1000m'},
                     'MEMORY_REQUEST':{'value':'1Gi'},'MEMORY_LIMIT':{'value':'2Gi'},
                     'JMETER_THREADS':{'value':'5'},'DURATION_SECONDS':{'value':'1800'},
                     'API_TOKEN':{'isSecret':True,'value':'never-show'}},'Definición de ejemplo'),
                   'warnings':['Datos simulados; no representan valores del banco.']}
    return AZURE.definition(kind,definition_id)

# Variables copiadas del portal: se interpretan como datos, sin ejecutar texto.
AWS_SESSIONS={}
AWS_SESSION_LOCK=threading.RLock()
AWS_KEYS={'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_REGION','AWS_DEFAULT_REGION'}
AZURE_KEYS={'AZURE_DEVOPS_ORG_URL','AZURE_DEVOPS_PROJECT','AZURE_DEVOPS_EXT_PAT'}


def parse_environment_block(text,allowed):
    if not isinstance(text,str) or not text.strip() or len(text)>100_000:
        raise ValueError('Pega un bloque de variables válido, de hasta 100 KB.')
    values={}
    for line_number,line in enumerate(text.splitlines(),1):
        line=line.strip()
        if not line or line.startswith('#') or re.match(r'(?i)^rem\s',line):continue
        for statement in line.split(';'):
            statement=statement.strip()
            if not statement:continue
            if re.match(r'(?i)^set\s+"',statement) and statement.endswith('"'):
                statement=re.sub(r'(?i)^set\s+"','',statement)[:-1]
            match=re.fullmatch(r'(?i)(?:(?:export|set)\s+|\$env:)?([A-Z_][A-Z_0-9]*)\s*=\s*(.*)',statement)
            if not match or match.group(1).upper() not in allowed:
                raise ValueError(f'Línea {line_number}: se admiten asignaciones de variables de credenciales. Para un script con comandos usa la conexión por archivo.')
            key,value=match.group(1).upper(),match.group(2).strip()
            if value[:1] in ('"',"'"):
                if len(value)<2 or value[-1]!=value[0]:raise ValueError(f'Línea {line_number}: comillas sin cerrar.')
                value=value[1:-1]
            if not value or '\x00' in value or len(value)>20000:
                raise ValueError(f'Línea {line_number}: valor vacío o no válido.')
            if any(symbol in value for symbol in ('$(', '`', '${')):
                raise ValueError(f'Línea {line_number}: pega el valor literal; no una expresión de la terminal.')
            if key in values and values[key]!=value:
                raise ValueError(f'Línea {line_number}: la misma variable aparece con dos valores distintos.')
            values[key]=value
    return values


def aws_process_env(context):
    with AWS_SESSION_LOCK:session=AWS_SESSIONS.get(context)
    if not session:return None
    env=dict(os.environ)
    for key in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_SECURITY_TOKEN',
                'AWS_PROFILE','AWS_DEFAULT_PROFILE','AWS_ROLE_ARN','AWS_WEB_IDENTITY_TOKEN_FILE'):
        env.pop(key,None)
    env.update(session['values'])
    return env


def aws_connection_status(context):
    """Comprueba la ruta de autenticación activa sin exponer credenciales."""
    context = str(context or '').strip()
    if DEMO:
        return {'connected': True, 'state': 'connected', 'source': 'demo',
                'context': context or 'demo-eks-qa', 'account': '000000000000',
                'arn': 'arn:aws:sts::000000000000:assumed-role/demo/eks-console',
                'message': 'Sesión de demostración activa. No se consultó AWS.'}
    if not context:
        return {'connected': False, 'state': 'no_context', 'source': 'none', 'context': '',
                'message': 'Selecciona un contexto de Kubernetes para verificar la conexión.'}
    try:
        context = resolve_context(context)
    except (RuntimeError, ValueError) as exc:
        return {'connected': False, 'state': 'no_context', 'source': 'none', 'context': context,
                'message': str(exc)}
    with AWS_SESSION_LOCK:
        temporary = context in AWS_SESSIONS
        loaded_at = AWS_SESSIONS.get(context, {}).get('loaded_at')
    if temporary:
        if not shutil.which('aws'):
            return {'connected': False, 'state': 'missing_aws_cli', 'source': 'temporary',
                    'context': context, 'message': 'AWS CLI no está disponible en el PATH.'}
        env = aws_process_env(context) or dict(os.environ)
        env['AWS_PAGER'] = ''
        try:
            identity = json.loads(run_command(
                ['aws', 'sts', 'get-caller-identity', '--output', 'json', '--no-cli-pager'],
                timeout=10, env=env))
            return {'connected': True, 'state': 'connected', 'source': 'temporary',
                    'context': context, 'account': identity.get('Account', ''),
                    'arn': identity.get('Arn', ''), 'loaded_at': loaded_at,
                    'message': 'Credenciales temporales verificadas por AWS.'}
        except (RuntimeError, ValueError) as exc:
            return {'connected': False, 'state': 'invalid_credentials', 'source': 'temporary',
                    'context': context, 'message': str(exc)}
    if not shutil.which('kubectl'):
        return {'connected': False, 'state': 'missing_kubectl', 'source': 'cli',
                'context': context, 'message': 'kubectl no está disponible en el PATH.'}
    try:
        # kubectl respeta el perfil/exec configurado en kubeconfig (SSO, SAML o AWS CLI).
        kubectl('version', '--request-timeout=8s', '-o', 'json', context=context, timeout=10)
        profile = os.environ.get('AWS_PROFILE') or os.environ.get('AWS_DEFAULT_PROFILE') or ''
        return {'connected': True, 'state': 'connected', 'source': 'cli', 'context': context,
                'profile': profile, 'message': 'La sesión local puede autenticarse contra el clúster.'}
    except RuntimeError as exc:
        return {'connected': False, 'state': 'login_required', 'source': 'cli', 'context': context,
                'message': str(exc)}


def aws_block_connect(data):
    values=parse_environment_block(data.get('block',''),AWS_KEYS)
    required={'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN'}
    if not required.issubset(values):
        raise ValueError('El bloque temporal debe incluir AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY y AWS_SESSION_TOKEN.')
    context=str(data.get('context','')).strip()
    if not context:raise ValueError('Selecciona primero el contexto EKS al que corresponde el bloque.')
    if DEMO:
        return {'loaded': True, 'context': context, 'demo': True,
                'status': aws_connection_status(context)}
    context=resolve_context(context)
    with AWS_SESSION_LOCK:AWS_SESSIONS[context]={'values':values,'loaded_at':time.time()}
    status = aws_connection_status(context)
    if not status.get('connected'):
        with AWS_SESSION_LOCK:
            AWS_SESSIONS.pop(context, None)
        raise ValueError('AWS rechazó las credenciales temporales: ' + status.get('message', 'verifica el bloque pegado.'))
    return {'loaded': True, 'context': context, 'demo': False, 'status': status}


def azure_block_connect(data):
    block=str(data.get('block','')).strip()
    if not block:return AZURE.configure(data)
    values=parse_environment_block(block,AZURE_KEYS)
    return AZURE.configure({'organization':values.get('AZURE_DEVOPS_ORG_URL') or data.get('organization'),
                            'project':values.get('AZURE_DEVOPS_PROJECT') or data.get('project'),
                            'auth':'pat','token':values.get('AZURE_DEVOPS_EXT_PAT','')})
