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

BASE = Path(__file__).resolve().parent
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
                'kubectl': True, 'aws': True}
    info = {'contexts': [], 'current': '', 'profiles': profile_names(),
            'profile': os.environ.get('AWS_PROFILE') or os.environ.get('AWS_DEFAULT_PROFILE') or '',
            'scripts': discover_scripts(), 'connected_script': Path(CONNECTED_SCRIPT).name if CONNECTED_SCRIPT else '',
            'demo': False, 'kubectl': bool(shutil.which('kubectl')), 'aws': bool(shutil.which('aws')), 'error': None}
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


def pod_record(item, metrics):
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


def demo_snapshot(namespace, context):
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


def pod_content(kind, namespace, pod, context, container='', previous=False):
    scope(namespace, False); validate_name(pod)
    if container: validate_name(container)
    if DEMO:
        if kind == 'logs': return {'text': '[DATOS SIMULADOS]\n2026-09-25 INFO  Servicio iniciado\n2026-09-25 INFO  Documento procesado · 284 ms\n2026-09-25 WARN  Ejemplo de diagnóstico; no corresponde al banco.'}
        return {'events': [{'type':'Warning', 'reason':'BackOff', 'message':'[Simulado] Back-off restarting failed container', 'count':6, 'time':'2026-09-25T14:30:00Z'}]}
    context = resolve_context(context)
    if kind == 'logs':
        args = ['logs', '-n', namespace, pod, '--tail=200', '--limit-bytes=180000', '--timestamps=true']
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


def safe_variables(raw, origin):
    rows=[]
    for name, variable in (raw or {}).items():
        var=variable if isinstance(variable,dict) else {'value':variable}
        protected=bool(var.get('isSecret')) or bool(re.search(r'password|passwd|secret|token|credential|connectionstring|api.?key|private.?key|certificate',name,re.I))
        value='•••• Protegida' if protected else str(var.get('value') if var.get('value') is not None else '')[:2000]
        rows.append({'name':name,'value':value,'secret':protected,'origin':origin})
    return rows


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl): return None


class AzureClient:
    def __init__(self):
        self.lock=threading.RLock();self.organization='';self.project='';self.auth='';self.token='';self.expires=0

    def configure(self,data):
        org=str(data.get('organization','')).strip()
        if org.startswith('https://dev.azure.com/'):
            org=urlsplit(org).path.strip('/').split('/')[0]
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}',org):
            raise ValueError('Indica la organización de Azure DevOps Services, por ejemplo https://dev.azure.com/mi-organizacion.')
        project=str(data.get('project','')).strip()
        if not project or len(project)>150 or any(c in project for c in '\r\n/'):
            raise ValueError('Indica el nombre o ID de tu proyecto.')
        auth=data.get('auth','entra');token=str(data.get('token','')).strip()
        if auth not in ('entra','pat'): raise ValueError('Método de autenticación inválido.')
        if auth=='pat' and not 10<=len(token)<=2048: raise ValueError('Introduce un PAT válido en el campo local.')
        with self.lock:
            self.organization=org;self.project=project;self.auth=auth;self.token=token;self.expires=0
        # Verifica acceso a ese proyecto sin pedir un listado global.
        try:
            self.read('core','projects/'+quote(project,safe=''),project=False)
        except Exception:
            self.disconnect();raise
        return self.summary()

    def disconnect(self):
        with self.lock: self.organization='';self.project='';self.token='';self.auth='';self.expires=0

    def summary(self):
        with self.lock:
            return {'connected':bool(self.organization),'organization':self.organization,'project':self.project,'auth':self.auth}

    def read(self,kind,path,params=None,project=True):
        with self.lock:
            if not self.organization: raise ValueError('Conecta Azure DevOps primero.')
            if self.auth=='entra' and time.time()>=self.expires:
                try:
                    self.token=run_command(['az','account','get-access-token','--resource','499b84ac-1321-427f-aa17-267ca6975798','--query','accessToken','-o','tsv'],timeout=20).strip()
                except RuntimeError:
                    raise RuntimeError('No se pudo usar Microsoft Entra. Inicia sesión con az login en esta terminal o usa un PAT permitido por tu organización.') from None
                self.expires=time.time()+2400
            header=('Basic '+base64.b64encode((':'+self.token).encode()).decode()) if self.auth=='pat' else 'Bearer '+self.token
            host='vsrm.dev.azure.com' if kind=='release' else 'dev.azure.com'
            url='https://'+host+'/'+quote(self.organization,safe='')+'/'
            if project: url+=quote(self.project,safe='')+'/'
            url+='_apis/'+('' if kind=='core' else kind+'/')+path
        query=dict(params or {});query['api-version']='7.1'
        request=urllib.request.Request(url+'?'+urlencode(query),headers={'Authorization':header,'Accept':'application/json'})
        try:
            opener=urllib.request.build_opener(NoRedirect())
            with opener.open(request,timeout=20) as response:
                raw=response.read(6_000_001)
                if len(raw)>6_000_000: raise RuntimeError('Respuesta de Azure demasiado grande; usa un ID de pipeline.')
                value=json.loads(raw)
                if response.headers.get('x-ms-continuationtoken'):
                    value['_has_more']=True
                return value
        except urllib.error.HTTPError as exc:
            messages={401:'Sesión o token inválido/expirado.',403:'Tu usuario no tiene permiso para este recurso.',404:'Recurso no encontrado o no visible para tu usuario.'}
            raise RuntimeError('Azure DevOps HTTP '+str(exc.code)+': '+messages.get(exc.code,'No fue posible leer este recurso.')) from None
        except (urllib.error.URLError,TimeoutError,ssl.SSLError):
            raise RuntimeError('No se pudo conectar a Azure DevOps. Revisa VPN, proxy y certificados de tu equipo.') from None
        except json.JSONDecodeError:
            raise RuntimeError('Azure no devolvió JSON; revisa sesión, proxy y URL de la organización.') from None

    def definitions(self):
        definitions=[];warnings=[]
        for kind in ['build','release']:
            try:
                data=self.read(kind,'definitions',{'$top':100})
                definitions.extend({'id':p['id'],'name':p.get('name',''),'kind':kind} for p in data.get('value',[]))
                if data.get('_has_more'): warnings.append(kind+': se muestran los primeros 100. Puedes indicar otro ID manualmente.')
            except (RuntimeError,ValueError) as exc: warnings.append(kind+': '+str(exc))
        return {'definitions':definitions,'warnings':warnings,**self.summary()}

    def definition(self,kind,definition_id):
        if kind not in ('build','release') or not str(definition_id).isdigit(): raise ValueError('Selecciona tipo e ID de pipeline.')
        data=self.read(kind,'definitions/'+str(definition_id));variables=safe_variables(data.get('variables'),'Definición global')
        environments=[];groups=set();warnings=[]
        def collect_groups(items):
            for g in items or []:
                gid=g.get('id') if isinstance(g,dict) else g
                if str(gid).isdigit(): groups.add(int(gid))
        collect_groups(data.get('variableGroups'))
        for env in data.get('environments') or []:
            environments.append({'id':env['id'],'name':env['name']})
            variables+=safe_variables(env.get('variables'),'Ambiente: '+env.get('name',''))
            collect_groups(env.get('variableGroups'))
        for gid in sorted(groups)[:20]:
            try:
                group=self.read('distributedtask','variablegroups/'+str(gid))
                variables+=safe_variables(group.get('variables'),'Grupo: '+group.get('name',str(gid)))
            except RuntimeError as exc: warnings.append('Grupo '+str(gid)+': '+str(exc))
        if len(groups)>20: warnings.append('Se consultaron 20 grupos; hay más grupos vinculados.')
        if kind=='build' and (data.get('process') or {}).get('type')==2:
            warnings.append('Las variables declaradas en YAML, templates o calculadas en ejecución no están expandidas en esta API. Se muestran las variables de definición y grupos accesibles.')
        return {'id':data['id'],'name':data.get('name'),'kind':kind,'environments':environments,
                'variables':variables,'warnings':warnings}

    def poll(self,selection,tracked=None):
        kind=selection['kind'];definition=selection['definition_id'];stage=selection.get('stage','').strip()
        if kind=='build':
            builds=[self.read('build','builds/'+str(tracked['id']))] if tracked else self.read('build','builds',{'definitions':definition,'statusFilter':'inProgress','$top':20}).get('value',[])
            matches=[]
            for build in builds:
                active=build.get('status')=='inProgress';phase=None
                if stage:
                    timeline=self.read('build','builds/'+str(build['id'])+'/timeline')
                    stages=[r for r in timeline.get('records') or [] if r.get('type')=='Stage' and stage.lower() in (r.get('name','')+' '+r.get('identifier','')).lower()]
                    if len(stages)>1: raise RuntimeError('El filtro coincide con varias etapas. Usa un nombre más específico.')
                    phase=stages[0] if stages else None
                    if tracked and not phase: raise RuntimeError('La etapa seguida no aparece en el timeline. Se conserva la captura sin confirmar su finalización.')
                    active=bool(phase and phase.get('state')=='inProgress')
                entry={'id':build['id'],'key':'build:'+str(build['id'])+':'+stage,'active':active,
                       'status':phase.get('state') if phase else build.get('status'),
                       'result':phase.get('result') if phase else build.get('result'),
                       'name':build.get('buildNumber') or str(build['id']),
                       'start':phase.get('startTime') if phase else build.get('startTime'),
                       'end':phase.get('finishTime') if phase else build.get('finishTime')}
                if active or tracked: matches.append(entry)
        else:
            releases=[self.read('release','releases/'+str(tracked['id']))] if tracked else self.read('release','releases',{'definitionId':definition,'environmentStatusFilter':'inProgress','$expand':'environments','$top':20}).get('value',[])
            matches=[]
            for release in releases:
                for env in release.get('environments') or []:
                    if stage and str(env.get('definitionEnvironmentId'))!=stage and env.get('name','').lower()!=stage.lower(): continue
                    if tracked and env['id']!=tracked.get('environment_id'): continue
                    active=env.get('status')=='inProgress'
                    if active or tracked:
                        matches.append({'id':release['id'],'environment_id':env['id'],
                            'key':'release:'+str(release['id'])+':'+str(env['id']), 'active':active,
                            'status':env.get('status'),'result':env.get('status'),'name':release.get('name','')+' / '+env.get('name',''),
                            'start':next((s.get('startedOn') for s in reversed(env.get('deploySteps') or []) if s.get('startedOn')),None)})
        if len(matches)>1: raise RuntimeError('Hay varias ejecuciones activas. Selecciona un ambiente/etapa más específico o usa captura manual.')
        return matches[0] if matches else None

AZURE=AzureClient()


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
original_pod_record=pod_record

def pod_record(item,metrics):
    record=original_pod_record(item,metrics)
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

original_demo_snapshot=demo_snapshot

def demo_snapshot(namespace,context):
    data=original_demo_snapshot(namespace,context)
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


def aws_block_connect(data):
    values=parse_environment_block(data.get('block',''),AWS_KEYS)
    required={'AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN'}
    if not required.issubset(values):
        raise ValueError('El bloque temporal debe incluir AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY y AWS_SESSION_TOKEN.')
    context=str(data.get('context','')).strip()
    if not context:raise ValueError('Selecciona primero el contexto EKS al que corresponde el bloque.')
    if DEMO:return {'loaded':True,'context':context,'demo':True}
    context=resolve_context(context)
    with AWS_SESSION_LOCK:AWS_SESSIONS[context]={'values':values,'loaded_at':time.time()}
    return {'loaded':True,'context':context,'demo':False}


def azure_block_connect(data):
    block=str(data.get('block','')).strip()
    if not block:return AZURE.configure(data)
    values=parse_environment_block(block,AZURE_KEYS)
    return AZURE.configure({'organization':values.get('AZURE_DEVOPS_ORG_URL') or data.get('organization'),
                            'project':values.get('AZURE_DEVOPS_PROJECT') or data.get('project'),
                            'auth':'pat','token':values.get('AZURE_DEVOPS_EXT_PAT','')})


HTML = r'''

<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bancolombia · EKS Console</title>
<style>
:root{--bg:#f5f5f0;--surface:#fff;--text:#202623;--muted:#798079;--line:#e5e8df;--soft:#f7f8f4;--yellow:#fdda24;--green:#13795b;--green-bg:#e7f4ed;--red:#b94339;--red-bg:#fff0ed;--amber:#957100;--amber-bg:#fff7db;--nav:#181e1b;--shadow:0 4px 20px #16221805;color-scheme:light}
:root.dark{--bg:#131916;--surface:#1d2520;--text:#eef2eb;--muted:#a5b1a7;--line:#354038;--soft:#252f28;--green:#84d8ad;--green-bg:#203e2e;--red:#ffaca3;--red-bg:#452b2b;--amber:#f2d96d;--amber-bg:#423c22;--nav:#0d120f;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;font-family:Inter,"Segoe UI",Arial,sans-serif;font-size:14px;background:var(--bg);color:var(--text)}button,input,select{font:inherit}button{cursor:pointer}button:disabled{cursor:wait;opacity:.55}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #c0a600;outline-offset:3px}button,input,select{border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--text)}button{padding:10px 14px;font-weight:600}button:hover{filter:brightness(.97)}input,select{padding:11px 12px;min-width:0;width:100%}h1,h2,h3,p{margin:0}h1{font-size:30px;font-weight:650;letter-spacing:-1.1px}h2{font-size:17px;letter-spacing:-.3px}h3{font-size:14px}svg.icon{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0}.muted{color:var(--muted)}.small{font-size:12px}.row{display:flex;align-items:center;gap:10px}.spread{justify-content:space-between}.stack{display:grid;gap:7px}.yellow{background:var(--yellow);color:#202519;border-color:var(--yellow)}.hidden,[hidden]{display:none!important}
aside.sidebar{position:fixed;inset:0 auto 0 0;width:224px;background:var(--nav);color:#c5cec5;padding:34px 20px 24px;display:flex;flex-direction:column;z-index:5}.brand{font-size:26px;letter-spacing:-1.4px;font-weight:750;color:#fff;padding-left:10px}.brand-line{display:block;width:38px;height:5px;background:var(--yellow);border-radius:8px;transform:rotate(-12deg);margin:9px 0 0 1px}.product{margin:44px 10px 15px;font-size:10px;letter-spacing:2px;color:#919f93}.nav{background:transparent;border:0;color:#aebbb0;text-align:left;display:flex;align-items:center;gap:12px;margin:3px 0;padding:13px 14px;width:100%;font-size:13px}.nav.active{background:var(--yellow);color:#202718}.nav:hover{background:#2e392f;color:#fff}.nav.active:hover{background:var(--yellow);color:#202718}.sidebar-bottom{margin-top:auto}.local-chip{padding:16px;border:1px solid #39453b;border-radius:12px;background:#202923}.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:currentColor}.local-chip .row{color:#e5eee3;font-size:12px}.local-chip p{margin-top:7px;font-size:11px;line-height:1.6;color:#89998e}.proposal{font-size:10px;color:#7a8a7f;line-height:1.6;margin:20px 12px 0}
.main{margin-left:224px}.topbar{height:77px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 36px;background:var(--surface)}.breadcrumb{font-size:12px;color:var(--muted);display:flex;gap:12px;align-items:center}.breadcrumb strong{color:var(--text);font-weight:500}.icon-button{width:35px;height:35px;padding:7px;display:grid;place-items:center}.avatar{display:grid;place-items:center;background:var(--soft);border:1px solid var(--line);border-radius:50%;font-size:10px;font-weight:700;width:33px;height:33px}.status-chip{display:inline-flex;align-items:center;gap:7px;border-radius:50px;padding:7px 10px;background:var(--soft);color:var(--muted);font-size:11px;font-weight:600}.status-chip.good{background:var(--green-bg);color:var(--green)}.status-chip.danger{background:var(--red-bg);color:var(--red)}.status-chip.warning{background:var(--amber-bg);color:var(--amber)}
.content{padding:31px 36px 25px;max-width:1800px;margin:auto}.page-head{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:27px}.eyebrow{font-size:10px;letter-spacing:1.8px;color:var(--muted);font-weight:600;margin-bottom:9px}.subtitle{color:var(--muted);margin-top:9px;font-size:13px}.btn-with-icon{display:inline-flex;align-items:center;gap:8px}.filterbar{display:grid;grid-template-columns:minmax(180px,1.05fr) minmax(180px,1.1fr) minmax(150px,.75fr) minmax(170px,1fr);gap:15px;background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:18px;margin-bottom:21px;box-shadow:var(--shadow)}label.field{display:grid;gap:7px;font-size:10px;font-weight:650;color:var(--muted);text-transform:uppercase;letter-spacing:.8px}.field input,.field select{font-size:12px;font-weight:400;text-transform:none;letter-spacing:0;background:var(--soft)}
.notice{border-radius:9px;padding:11px 14px;background:var(--amber-bg);color:var(--amber);margin:0 0 16px;font-size:12px;line-height:1.55}.notice.error{background:var(--red-bg);color:var(--red)}.demo-notice{border-radius:0;background:var(--yellow);color:#272b1e;margin:0;padding:8px 36px;font-size:11px;font-weight:650;letter-spacing:.3px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:17px;margin-bottom:22px}.card{border:1px solid var(--line);border-radius:13px;background:var(--surface);padding:19px 20px;box-shadow:var(--shadow)}.card-top{display:flex;justify-content:space-between;align-items:center;font-size:12px;color:var(--muted)}.stat-icon{display:grid;place-items:center;width:33px;height:33px;border-radius:9px;background:var(--soft);color:var(--text)}.stat-icon.green{background:var(--green-bg);color:var(--green)}.stat-icon.red{background:var(--red-bg);color:var(--red)}.stat-icon.amber{background:var(--amber-bg);color:var(--amber)}.stat-value{font-size:37px;font-weight:650;letter-spacing:-1.6px;line-height:1.3;margin:7px 0}.stat-note{color:var(--muted);font-size:10px}.stat-value.green{color:var(--green)}.stat-value.red{color:var(--red)}.stat-value.amber{color:var(--amber)}
.summary{display:grid;grid-template-columns:1.4fr 1fr;gap:18px;margin-bottom:22px}.panel{border:1px solid var(--line);background:var(--surface);border-radius:13px;box-shadow:var(--shadow);overflow:hidden}.panel-pad{padding:20px 22px}.health-body{display:flex;align-items:center;gap:28px;margin-top:21px}.ring{width:100px;height:100px;border-radius:50%;display:grid;place-items:center;background:var(--line);flex-shrink:0}.ring-center{height:80px;width:80px;display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:50%;background:var(--surface)}.ring strong{font-size:23px;letter-spacing:-1px}.ring span{font-size:9px;color:var(--muted);margin-top:3px}.services{display:grid;gap:13px;flex:1;min-width:0}.service-row{font-size:11px}.service-title{overflow:hidden;text-overflow:ellipsis;max-width:75%;white-space:nowrap}.bar{height:5px;background:var(--line);border-radius:20px;overflow:hidden;margin-top:6px}.bar span{height:100%;display:block;background:var(--green);border-radius:20px}.resource-body{display:grid;grid-template-columns:1fr 1fr;gap:25px;margin-top:27px}.resource-label{color:var(--muted);font-size:11px}.resource-value{font-size:27px;font-weight:600;letter-spacing:-.9px;margin:9px 0 7px}.resource-value small{font-size:12px;color:var(--muted);font-weight:400;letter-spacing:0}.resource-caption{color:var(--muted);font-size:10px;line-height:1.5}.resource-line{margin-top:15px;height:4px;width:100%;border-radius:5px;background:var(--line)}.resource-line span{display:block;background:var(--yellow);height:4px;border-radius:5px}.resource-body>div+div{border-left:1px solid var(--line);padding-left:24px}
.table-header{padding:21px 22px 15px;display:flex;align-items:center;justify-content:space-between;gap:15px}.count{border:1px solid var(--line);background:var(--soft);padding:3px 7px;border-radius:5px;font-size:10px;margin-left:7px;color:var(--muted);vertical-align:middle}.segment{display:flex;background:var(--soft);padding:3px;border-radius:8px}.segment button{border:0;background:transparent;padding:7px 10px;font-size:11px;color:var(--muted);font-weight:500}.segment button.active{background:var(--surface);box-shadow:0 1px 4px #0001;color:var(--text)}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th{text-align:left;padding:11px 16px;font-size:10px;font-weight:600;color:var(--muted);border-block:1px solid var(--line);background:var(--soft)}th:first-child,td:first-child{padding-left:22px}td{border-bottom:1px solid var(--line);padding:14px 16px;font-size:11px}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:var(--soft)}.pod-button{padding:0;border:0;background:transparent;color:var(--text);display:block;text-align:left;font-size:11px;font-weight:600}.pod-meta{font-size:10px;color:var(--muted);margin-top:5px}.state{display:inline-flex;align-items:center;gap:5px;padding:5px 7px;border-radius:5px;font-size:10px;background:var(--soft);color:var(--muted)}.state.good{background:var(--green-bg);color:var(--green)}.state.danger{background:var(--red-bg);color:var(--red)}.state.warning{background:var(--amber-bg);color:var(--amber)}.mini-bar{width:54px;height:3px;background:var(--line);border-radius:3px;margin-top:6px}.mini-bar span{height:3px;display:block;border-radius:3px;background:#a2ac9a}.restart.bad{color:var(--red);font-weight:700}.table-footer{padding:13px 22px;border-top:1px solid var(--line);color:var(--muted);font-size:10px;display:flex;justify-content:space-between}.empty{text-align:center;padding:38px;white-space:normal;color:var(--muted);font-size:12px}.bottom{display:flex;justify-content:space-between;color:var(--muted);font-size:10px;margin-top:20px;line-height:1.8}.bottom code{font-size:10px}.stale{opacity:.65}
.dialog{padding:0;background:var(--surface);color:var(--text);border:1px solid var(--line);border-radius:17px;box-shadow:0 30px 100px #0005;width:min(720px,94vw);max-height:90vh}.dialog::backdrop{background:#15221980;backdrop-filter:blur(3px)}.dialog-head{padding:24px 26px 20px;border-bottom:1px solid var(--line)}.dialog-body{padding:23px 26px;overflow:auto}.dialog h2{font-size:21px}.connection-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:22px}.connection-tile{background:var(--soft);padding:15px;border:1px solid var(--line);border-radius:10px}.connection-tile p{font-size:11px;color:var(--muted);margin-top:7px;overflow-wrap:anywhere}.connection-tile strong{font-size:12px}.instructions{color:var(--muted);font-size:12px;line-height:1.8;margin:12px 0}.script-item{padding:12px 14px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px}.script-item strong{font-size:12px}.script-item p{font-size:10px;color:var(--muted);margin-top:5px;overflow-wrap:anywhere}pre.code{font-family:Consolas,monospace;font-size:12px;background:var(--soft);border:1px solid var(--line);padding:13px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7}.drawer{inset:0 0 0 auto;margin:0;width:min(700px,94vw);height:100vh;max-height:100vh;border-radius:16px 0 0 16px}.drawer .dialog-head h2{font-size:18px;overflow-wrap:anywhere;max-width:580px}.tabbar{display:flex;gap:16px;border-bottom:1px solid var(--line);padding:0 26px}.tabbar button{background:transparent;border:0;border-radius:0;color:var(--muted);font-size:12px;padding:15px 0}.tabbar button.active{border-bottom:3px solid var(--yellow);color:var(--text)}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:17px;font-size:12px;margin-bottom:25px}.detail-grid dt{font-size:10px;color:var(--muted);margin-bottom:6px}.detail-grid dd{margin:0;overflow-wrap:anywhere;line-height:1.6}.container-item{border:1px solid var(--line);border-radius:10px;margin:10px 0;padding:16px}.container-item p{font-family:Consolas,monospace;font-size:11px;color:var(--muted);margin-top:10px;overflow-wrap:anywhere}.log-options{display:flex;gap:10px;margin-bottom:14px;align-items:center;font-size:11px}.log-options select{max-width:230px}.log-options input{width:auto}.logs{background:#131c17;color:#d0e2d4;padding:18px;border-radius:10px;font:11px/1.8 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;min-height:240px;max-height:63vh;overflow:auto}.event{padding:16px 0;border-bottom:1px solid var(--line);font-size:12px;line-height:1.6}.event p{margin-top:8px}.event small{display:block;color:var(--muted);margin-top:8px}
@media(min-width:1500px){.content{padding:35px 44px}.card{padding:24px}.stat-value{font-size:42px}td{padding-block:17px}}
@media(max-width:1120px){aside.sidebar{width:180px;padding-inline:13px}.brand{font-size:23px}.main{margin-left:180px}.content{padding:25px 22px}.topbar{padding-inline:22px}.filterbar{grid-template-columns:1fr 1fr}.cards{gap:10px}.card{padding:15px}.summary{grid-template-columns:1fr 1fr}.health-body{gap:18px}.ring{width:80px;height:80px}.ring-center{width:64px;height:64px}.resource-body{gap:12px}.resource-body>div+div{padding-left:12px}}
@media(max-width:760px){aside.sidebar{position:static;width:auto;height:64px;padding:12px 18px;display:flex;flex-direction:row;justify-content:space-between;align-items:center}.brand{font-size:22px;display:flex;align-items:center;gap:12px}.brand-line{width:24px;margin:0}.sidebar .product,.sidebar nav,.sidebar-bottom{display:none}.main{margin:0}.topbar{height:55px;padding-inline:16px}.breadcrumb{font-size:10px;gap:7px}.content{padding:23px 16px}.page-head{align-items:start;margin-bottom:22px}h1{font-size:24px}.page-head .subtitle{font-size:11px;max-width:210px;line-height:1.6}.page-head button{font-size:11px;padding:9px}.cards{grid-template-columns:1fr 1fr}.summary{grid-template-columns:1fr}.filterbar{padding:12px;gap:12px}.filterbar input,.filterbar select{font-size:11px}.table-header{padding:16px;align-items:start}.segment button{font-size:10px;padding:7px}.table-footer{flex-direction:column;gap:7px}.bottom{flex-direction:column;gap:5px}.demo-notice{padding-inline:16px}.connection-grid{grid-template-columns:1fr}.detail-grid{grid-template-columns:1fr}.dialog-body{padding:20px}}
.section-heading{margin-bottom:20px}.section-heading p,.table-header p{margin-top:8px}.perf-toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:22px}.perf-toolbar button{font-size:12px}.perf-toolbar span{flex-basis:100%;margin-top:3px}.perf-cards .stat-value{font-size:29px}.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:22px 0}.chart-note{margin-top:8px;font-size:11px}.chart{height:220px;margin-top:20px}.chart svg{width:100%;height:185px;display:block;overflow:visible}.chart-legend{display:flex;gap:17px;font-size:10px;color:var(--muted);margin-top:8px}.chart-legend span:before{content:'';display:inline-block;width:16px;height:3px;background:var(--legend-color);vertical-align:middle;margin-right:6px}.chart-empty{display:grid;place-items:center;height:200px;border:1px dashed var(--line);border-radius:8px;color:var(--muted);font-size:12px;padding:25px;text-align:center}.perf-block{margin:22px 0}.hpa-list{display:grid;gap:12px;margin-top:20px}.hpa-item{padding:12px;border:1px solid var(--line);border-radius:9px;font-size:12px}.hpa-item p{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.6}.hpa-item .bar span{background:var(--yellow)}.slo-fields{display:grid;grid-template-columns:1.3fr 1fr 1fr auto;align-items:end;gap:12px;margin:20px 0 15px}.button-file{background:var(--yellow);color:#23261b;border-radius:8px;padding:11px 14px;font-weight:600;font-size:12px;cursor:pointer}.jtl-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin:20px 0}.jtl-stat{border:1px solid var(--line);padding:16px;border-radius:10px}.jtl-stat strong{font-size:23px;display:block;margin:9px 0}.jtl-stat span{font-size:10px;color:var(--muted)}.findings{display:grid;gap:12px;margin:22px 0}.finding{border:1px solid var(--line);border-left:4px solid var(--muted);border-radius:8px;padding:15px 17px;line-height:1.65;font-size:12px}.finding.danger{border-left-color:var(--red)}.finding.warning{border-left-color:#d4a600}.finding.good{border-left-color:var(--green)}.finding strong{font-size:13px}.finding p{margin-top:5px;color:var(--muted)}.finding .action{color:var(--text)}.inventory-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}.inventory-card{padding:20px;border:1px solid var(--line);border-radius:12px;background:var(--surface)}.inventory-card h3{margin:0 0 15px}.inventory-card strong{font-size:32px;display:block;margin:15px 0}.inventory-card p{font-size:11px;color:var(--muted);line-height:1.7;overflow-wrap:anywhere}.inventory-card details{font-size:11px;margin-top:10px}.azure-layout{display:grid;grid-template-columns:1fr 1fr;gap:20px}.form-stack{display:grid;gap:17px;margin-top:22px}.form-stack .field{font-size:10px}.form-stack input,.form-stack select{background:var(--soft)}.grow{flex:1;min-width:0}.environment-bar{display:flex;align-items:center;gap:10px;margin-bottom:15px;font-size:11px;color:var(--muted)}.environment-bar select{max-width:300px;font-size:12px;padding:8px}.environment-bar button{font-size:11px;padding:8px}.var-value{white-space:pre-wrap;word-break:break-word;max-width:420px;font-family:Consolas,monospace;font-size:11px}.views-tabs{display:none}.inventory-card pre{white-space:pre-wrap;font-size:10px;overflow-wrap:anywhere}
@media(max-width:1120px){.slo-fields{grid-template-columns:1fr 1fr}.inventory-grid{grid-template-columns:1fr 1fr}.jtl-stats{grid-template-columns:repeat(3,1fr)}}
@media(max-width:760px){.chart-grid,.azure-layout{grid-template-columns:1fr}.inventory-grid{grid-template-columns:1fr 1fr;gap:10px}.inventory-card{padding:14px}.jtl-stats{grid-template-columns:1fr 1fr}.slo-fields{grid-template-columns:1fr 1fr}.views-tabs{display:flex;gap:5px;overflow:auto;margin-bottom:20px}.views-tabs button{font-size:11px;padding:8px 10px}.section-heading{align-items:start}.environment-bar{flex-wrap:wrap}.environment-bar select{flex:1}.perf-toolbar button{font-size:11px}.chart{height:205px}.chart svg{height:170px}}

textarea.credential-block{width:100%;min-height:155px;resize:vertical;font:12px/1.7 Consolas,monospace;padding:13px;border:1px solid var(--line);border-radius:9px;background:var(--soft);color:var(--text)}.credential-section{margin-bottom:26px;padding-bottom:22px;border-bottom:1px solid var(--line)}.credential-section h3{margin-bottom:10px}.credential-section label{margin-top:12px}
</style></head><body>
<svg style="display:none" aria-hidden="true"><defs>
<symbol id="i-grid" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></symbol>
<symbol id="i-cube" viewBox="0 0 24 24"><path d="m12 2 9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10M7.5 4.5l9 5"/></symbol>
<symbol id="i-link" viewBox="0 0 24 24"><path d="m9 15 6-6M8 17l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 7l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(0 -1) scale(.95)"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></symbol>
<symbol id="i-alert" viewBox="0 0 24 24"><path d="m12 3 10 17H2zM12 9v5M12 17v.1"/></symbol>
<symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20 7a9 9 0 1 0 1 8M20 3v5h-5"/></symbol>
<symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 14A9 9 0 0 1 10 3a9 9 0 1 0 10 11Z"/></symbol>
<symbol id="i-close" viewBox="0 0 24 24"><path d="m6 6 12 12M6 18 18 6"/></symbol>
<symbol id="i-pause" viewBox="0 0 24 24"><path d="M8 5v14M16 5v14"/></symbol>
<symbol id="i-arrow" viewBox="0 0 24 24"><path d="m9 5 7 7-7 7"/></symbol>
</defs></svg>
<aside class="sidebar"><div class="brand">bancolombia<span class="brand-line"></span></div><div class="product">PLATAFORMA EKS</div><nav aria-label="Navegación principal"><button class="nav active" id="navOverview"><svg class="icon"><use href="#i-grid"/></svg>Vista general</button><button class="nav" id="navPods"><svg class="icon"><use href="#i-cube"/></svg>Pods y servicios</button><button class="nav" id="navPerformance"><svg class="icon"><use href="#i-grid"/></svg>Performance</button><button class="nav" id="navInventory"><svg class="icon"><use href="#i-cube"/></svg>Recursos por ambiente</button><button class="nav" id="navAzure"><svg class="icon"><use href="#i-link"/></svg>Azure DevOps</button><button class="nav" id="navConnection"><svg class="icon"><use href="#i-link"/></svg>Conexión AWS</button></nav><div class="sidebar-bottom"><div class="local-chip"><div class="row"><span class="dot" style="color:#8cca9d"></span>Entorno local</div><p>Tu sesión. Tus permisos.<br>Consulta de solo lectura.</p></div><div class="proposal">PROPUESTA DE HERRAMIENTA INTERNA<br>EKS Console · versión 3.1</div></div></aside>
<div class="main"><div class="topbar"><div class="breadcrumb">Plataforma tecnológica <span>/</span><strong>Observabilidad</strong></div><div class="row"><span id="connectionState" class="status-chip"><span class="dot"></span><span>Detectando sesión</span></span><button id="theme" class="icon-button" title="Cambiar tema" aria-label="Cambiar tema"><svg class="icon"><use href="#i-moon"/></svg></button><div class="avatar" title="Sesión local">BC</div></div></div>
<div id="demoBanner" class="notice demo-notice" hidden>MODO DEMOSTRACIÓN · Todos los datos mostrados son simulados.</div>
<main class="content"><section class="page-head"><div><div class="eyebrow">CONTROL OPERATIVO</div><h1>Tu plataforma, en una vista.</h1><p class="subtitle">Salud y consumo de tus servicios en Kubernetes.</p></div><button id="connect" class="btn-with-icon yellow"><svg class="icon"><use href="#i-link"/></svg>Conexión AWS</button></section>
<div class="views-tabs"><button data-view="eks">EKS</button><button data-view="performance">Performance</button><button data-view="inventory">Recursos</button><button data-view="azure">Azure</button></div><div class="environment-bar"><span>AMBIENTE</span><select id="environment"><option value="">Sesión actual / personalizado</option></select><button id="manageEnvironments">Mis ambientes</button></div><div class="filterbar"><label class="field">Clúster / contexto<select id="context"><option>Detectando...</option></select></label><label class="field">Namespace<input id="namespace" placeholder="generaciondocumentaldigital-qa" spellcheck="false"></label><label class="field">Microservicio<select id="micro"><option value="">Todos los servicios</option></select></label><label class="field">Buscar pod<input id="search" placeholder="Nombre, nodo o estado..." spellcheck="false"></label></div>
<div id="notice" class="notice" hidden></div>
<div id="eksView"><section id="stats" class="cards"><article class="card"><div class="card-top">Pods detectados<span class="stat-icon"><svg class="icon"><use href="#i-cube"/></svg></span></div><div class="stat-value" id="total">—</div><div class="stat-note" id="scopeNote">Esperando conexión</div></article><article class="card"><div class="card-top">Listos para recibir tráfico<span class="stat-icon green"><svg class="icon"><use href="#i-check"/></svg></span></div><div class="stat-value green" id="ready">—</div><div class="stat-note">Condición Ready de Kubernetes</div></article><article class="card"><div class="card-top">Requieren atención<span class="stat-icon red"><svg class="icon"><use href="#i-alert"/></svg></span></div><div class="stat-value red" id="attention">—</div><div class="stat-note" id="attentionNote">Errores, pendientes o no listos</div></article><article class="card"><div class="card-top">Reinicios acumulados<span class="stat-icon amber"><svg class="icon"><use href="#i-refresh"/></svg></span></div><div class="stat-value amber" id="restarts">—</div><div class="stat-note">Desde la creación de estos pods</div></article></section>
<section class="summary"><article class="panel panel-pad"><div class="row spread"><h2>Salud de los servicios</h2><span class="muted small">Ahora</span></div><div class="health-body"><div class="ring" id="healthRing"><div class="ring-center"><strong id="healthPercent">—</strong><span>PODS READY</span></div></div><div class="services" id="services"><span class="muted small">Los servicios aparecerán al conectar.</span></div></div></article><article class="panel panel-pad"><div class="row spread"><h2>Consumo de recursos</h2><span class="muted small" id="metricsScope">Pods filtrados</span></div><div class="resource-body"><div><div class="resource-label">CPU utilizada</div><div class="resource-value"><span id="cpu">—</span> <small>cores</small></div><div class="resource-caption" id="cpuCaption">Esperando métricas</div><div class="resource-line"><span id="cpuBar" style="width:0"></span></div></div><div><div class="resource-label">Memoria utilizada</div><div class="resource-value"><span id="memory">—</span> <small>GiB</small></div><div class="resource-caption" id="memoryCaption">Esperando métricas</div><div class="resource-line"><span id="memoryBar" style="width:0"></span></div></div></div></article></section>
<section class="panel" id="podPanel"><div class="table-header"><div><h2>Pods <span class="count" id="podCount">—</span></h2><p class="muted small" style="margin-top:7px">Selecciona un pod para ver su diagnóstico.</p></div><div class="row"><div class="segment" aria-label="Filtrar estado"><button id="allStates" class="active">Todos</button><button id="problemStates">Con alertas</button></div><button id="reload" class="icon-button" title="Actualizar ahora" aria-label="Actualizar ahora"><svg class="icon"><use href="#i-refresh"/></svg></button></div></div><div class="table-scroll"><table><thead><tr><th>Pod / servicio</th><th>Estado</th><th>Ready</th><th>CPU</th><th>Memoria</th><th>Reinicios</th><th>Edad</th><th></th></tr></thead><tbody id="rows"><tr><td colspan="8" class="empty">Detectando tu configuración de Kubernetes...</td></tr></tbody></table></div><div class="table-footer"><span id="updated">Sin lecturas del clúster todavía</span><button id="auto" style="padding:0;background:transparent;border:0;color:var(--muted);font-size:10px">● Actualización automática · 15 s</button></div></section>
</div><section id="performanceView" hidden>
<div class="row spread section-heading"><div><h2>Performance</h2><p class="muted small">Capacidad aplicada, comportamiento bajo carga y resultados de la prueba.</p></div><span class="status-chip" id="perfState">Sin captura</span></div>
<div class="perf-toolbar"><button class="yellow" id="startCapture">Iniciar captura</button><button id="armCapture">Activar con Azure</button><button id="stopCapture">Finalizar captura</button><button id="exportPerf">Exportar análisis</button><span class="muted small" id="captureScope">Selecciona un ambiente de EKS.</span></div>
<div id="perfNotice" class="notice" hidden></div><div id="runInfo" class="notice" hidden></div>
<div class="cards perf-cards"><article class="card"><div class="card-top">CPU utilizada</div><div class="stat-value" id="perfCpu">—</div><div class="stat-note" id="perfCpuMeta">Request — · Limit — cores</div></article><article class="card"><div class="card-top">Memoria utilizada</div><div class="stat-value" id="perfMem">—</div><div class="stat-note" id="perfMemMeta">Request — · Limit — GiB</div></article><article class="card"><div class="card-top">Pods listos / observados</div><div class="stat-value green" id="perfReady">—</div><div class="stat-note" id="perfMeasured">Cobertura de métricas pendiente</div></article><article class="card"><div class="card-top">Muestras de esta ventana</div><div class="stat-value" id="perfSamples">—</div><div class="stat-note" id="perfDuration">Muestreo cada 15 s</div></article></div>
<div class="chart-grid"><article class="panel panel-pad"><h2>CPU · uso, requests y limits</h2><p class="muted small chart-note">Suma del ámbito seleccionado · cores</p><div id="cpuChart" class="chart"></div></article><article class="panel panel-pad"><h2>Memoria · uso, requests y limits</h2><p class="muted small chart-note">Suma del ámbito seleccionado · GiB</p><div id="memChart" class="chart"></div></article></div>
<div class="chart-grid"><article class="panel panel-pad"><h2>Disponibilidad y escalamiento</h2><p class="muted small chart-note">Pods observados frente a pods Ready</p><div id="podsChart" class="chart"></div></article><article class="panel panel-pad"><h2>Escaladores HPA</h2><p class="muted small chart-note">Objetivo, réplicas actuales y máximo</p><div id="hpaList" class="hpa-list"><span class="muted small">Esperando lectura.</span></div></article></div>
<section class="panel perf-block"><div class="table-header"><div><h2>Recursos aplicados por pod</h2><p class="muted small">Valores de Kubernetes. «—» significa no declarado o no disponible.</p></div></div><div class="table-scroll"><table><thead><tr><th>Pod</th><th>CPU uso</th><th>Request</th><th>Limit</th><th>Memoria uso</th><th>Request</th><th>Limit</th></tr></thead><tbody id="resourcesRows"></tbody></table></div><div class="table-footer">Asignaciones de contenedores residentes, o recursos del pod si existen. Los init normales y overhead pueden cambiar la reserva del scheduler; revísalos en el detalle.</div></section>
<section class="panel panel-pad perf-block"><div class="row spread"><div><h2>Resultados de carga · JMeter</h2><p class="muted small chart-note">Importa muestras de esta ejecución para analizar latencia y errores.</p></div><label class="button-file">Importar JTL / CSV<input id="jtlFile" type="file" accept=".jtl,.csv" hidden></label></div><div class="slo-fields"><label class="field">Etiqueta exacta (opcional)<input id="jtlLabel" placeholder="Ej.: Generar documento"></label><label class="field">Objetivo P95 · ms<input id="sloP95" type="number" min="1" value="1000"></label><label class="field">Máximo de errores · %<input id="sloErrors" type="number" min="0" max="100" step="0.1" value="1"></label><button id="reprocessJtl">Recalcular archivo</button></div><div id="jtlMessage" class="muted small">Los objetivos son editables. CSV con timeStamp, elapsed y success; hasta 10 MB.</div><div id="jtlStats" class="jtl-stats" hidden></div><div id="latencyChart" class="chart" hidden></div></section>
<section class="panel panel-pad perf-block"><div class="row spread"><h2>Análisis de la ejecución</h2><span class="muted small">Reglas verificables · sin enviar datos a una IA</span></div><div id="findings" class="findings"><p class="muted small">Inicia la observación para obtener hallazgos.</p></div><p class="muted small" id="coverageNote"></p></section>
</section>
<section id="inventoryView" hidden><div class="row spread section-heading"><div><h2>Recursos por ambiente</h2><p class="muted small">Consulta únicamente el contexto y namespace seleccionados.</p></div><button id="loadInventory" class="yellow">Consultar recursos</button></div><div id="inventoryNotice" class="notice" hidden></div><div id="inventoryGrid" class="inventory-grid"></div><p class="instructions">No se requiere listar clústeres de AWS ni todos los namespaces. Un permiso denegado se presenta por recurso y no bloquea las demás consultas.</p></section>
<section id="azureView" hidden><div class="row spread section-heading"><div><h2>Azure DevOps</h2><p class="muted small">Pipelines, ambientes y variables que tu usuario puede consultar.</p></div><span class="status-chip" id="azureState">Sin conexión</span></div><div class="azure-layout"><section class="panel panel-pad"><h2>Conecta tu proyecto</h2><form id="azureForm" class="form-stack"><label class="field">Organización<input id="azOrg" placeholder="https://dev.azure.com/tu-organizacion"></label><label class="field">Proyecto<input id="azProject" placeholder="Nombre o ID del proyecto"></label><label class="field">Autenticación<select id="azAuth"><option value="entra">Microsoft Entra · sesión de Azure CLI</option><option value="pat">Token de acceso personal · PAT</option></select></label><label class="field" id="patField" hidden>Token · solo durante esta sesión<input id="azToken" type="password" autocomplete="off" spellcheck="false"></label><p class="muted small" id="authHelp">Inicia sesión en tu terminal con az login. La herramienta usa esa sesión para solicitar acceso a Azure DevOps.</p><details><summary class="small">Pegar bloque Azure DevOps (opcional)</summary><p class="instructions">Puedes pegar las variables AZURE_DEVOPS_ORG_URL, AZURE_DEVOPS_PROJECT y AZURE_DEVOPS_EXT_PAT. También puedes completar los campos anteriores.</p><textarea id="azCredentialBlock" class="credential-block" autocomplete="off" spellcheck="false" placeholder='$env:AZURE_DEVOPS_ORG_URL="https://dev.azure.com/mi-organizacion"&#10;$env:AZURE_DEVOPS_PROJECT="Mi proyecto"&#10;$env:AZURE_DEVOPS_EXT_PAT="..."'></textarea></details><div class="row"><button class="yellow" type="submit" id="azConnect">Conectar</button><button type="button" id="azDisconnect">Desconectar</button></div></form><p class="instructions">Se necesita lectura del proyecto, Build y/o Release, y Variable Groups cuando aplique. El token queda en memoria del proceso; al cerrar se elimina.</p></section><section class="panel panel-pad"><h2>Pipeline de Performance</h2><div class="form-stack"><label class="field">Pipelines accesibles<select id="azDefinitions"><option value="">Conecta el proyecto para consultar</option></select></label><div class="row"><label class="field grow">Tipo<select id="azKind"><option value="build">Build / YAML</option><option value="release">Release clásico</option></select></label><label class="field grow">ID manual<input id="azId" inputmode="numeric" placeholder="ID de definición"></label></div><button id="azReadDefinition">Leer configuración del pipeline</button><label class="field">Ambiente de Release<select id="azEnvironment"><option value="">Elige un ambiente</option></select></label><label class="field">Etapa YAML (opcional)<input id="azStage" placeholder="Nombre de Stage; vacío = pipeline completo"></label><p class="muted small" id="azureBinding">El pipeline se vinculará al contexto, namespace y microservicio seleccionados arriba al activar la captura.</p><button id="azureArm" class="yellow">Activar captura automática</button></div></section></div><div id="azureNotice" class="notice" hidden></div><section class="panel perf-block"><div class="table-header"><div><h2>Variables de configuración <span id="variableCount" class="count">0</span></h2><p class="muted small">Origen explícito. Las variables secretas permanecen protegidas.</p></div><input id="variableSearch" placeholder="CPU, memory, replicas, threads..." style="max-width:270px"></div><div class="table-scroll"><table><thead><tr><th>Variable</th><th>Valor declarado</th><th>Origen</th></tr></thead><tbody id="variablesRows"><tr><td colspan="3" class="empty">Selecciona una definición.</td></tr></tbody></table></div></section></section>
<dialog class="dialog" id="environmentDialog"><div class="dialog-head row spread"><h2>Mis ambientes EKS</h2><button data-close="environmentDialog" class="icon-button" aria-label="Cerrar">×</button></div><div class="dialog-body"><p class="instructions">Guarda nombres y ámbitos conocidos. No necesitas permisos para listar todos los clústeres o namespaces.</p><div class="form-stack"><label class="field">Nombre del ambiente<input id="envName" placeholder="QA · Generación documental"></label><label class="field">Contexto de kubeconfig<input id="envContext" placeholder="Copia el nombre del contexto local"></label><label class="field">Namespace conocido<input id="envNamespace" placeholder="generaciondocumentaldigital-qa"></label><button id="saveEnvironment" class="yellow">Guardar ambiente</button></div><div id="savedEnvironments" style="margin-top:20px"></div><p class="instructions">Solo se guardan estas referencias en este navegador. No se almacenan contraseñas ni tokens.</p></div></dialog>

<footer class="bottom"><span id="contextFoot">Contexto local de Kubernetes</span><span>Propuesta visual · Bancolombia · Solo lectura</span></footer></main></div>
<dialog class="dialog" id="connectionDialog"><div class="dialog-head row spread"><div><div class="eyebrow">CONEXIÓN LOCAL</div><h2>Tu sesión de AWS, aquí.</h2></div><button class="icon-button" data-close="connectionDialog" aria-label="Cerrar conexión"><svg class="icon"><use href="#i-close"/></svg></button></div><div class="dialog-body"><div class="connection-grid" id="connectionInfo"></div><section class="credential-section"><h3>Pegar bloque de credenciales AWS</h3><p class="instructions">Pega las tres variables temporales del portal AWS. Se admiten formatos PowerShell ($env:), Bash (export) y CMD (set).</p><label class="field">Contexto EKS de este ambiente<input id="awsCredentialContext" placeholder="Selecciona el contexto arriba o escribe su nombre"></label><label class="field">Bloque completo<textarea id="awsCredentialBlock" class="credential-block" autocomplete="off" spellcheck="false" placeholder="$env:AWS_ACCESS_KEY_ID=&quot;...&quot;&#10;$env:AWS_SECRET_ACCESS_KEY=&quot;...&quot;&#10;$env:AWS_SESSION_TOKEN=&quot;...&quot;"></textarea></label><div class="row" style="margin-top:13px"><button id="applyAwsBlock" class="yellow">Aplicar bloque al ambiente</button><button id="removeAwsBlock">Quitar sesión pegada</button></div><div id="awsCredentialMessage" class="instructions"></div><p class="instructions">Las credenciales se mantienen en memoria y se usan para este contexto. Pega valores literales; los scripts con comandos siguen disponibles abajo.</p></section><h3>Script de conexión detectado</h3><p class="instructions">Se buscan archivos .ps1, .bat, .cmd y .sh junto a este tablero y en su carpeta scripts. La búsqueda identifica candidatos por su contenido.</p><div id="scriptList"></div><pre class="code" id="launchCommand"></pre><p class="instructions">Para usar el lanzador, cierra esta instancia con Ctrl+C y ejecuta el comando anterior desde su carpeta. El script abre tu inicio de sesión habitual; después inicia el tablero heredando esa sesión. Si el script necesita parámetros, ajústalo con su autor antes de usar el lanzador.</p><p class="instructions">Si ya te conectaste desde otra terminal, vuelve a detectar. Las variables temporales de esa otra terminal solo se heredan al iniciar Python desde ella.</p><button class="yellow btn-with-icon" id="detect"><svg class="icon"><use href="#i-refresh"/></svg>Volver a detectar sesión</button></div></dialog>
<dialog class="dialog drawer" id="podDialog"><div class="dialog-head"><div class="row spread"><div class="eyebrow">DETALLE DEL POD</div><button class="icon-button" data-close="podDialog" aria-label="Cerrar detalle"><svg class="icon"><use href="#i-close"/></svg></button></div><h2 id="podTitle"></h2><p id="podSubtitle" class="muted small" style="margin-top:10px"></p></div><div class="tabbar"><button class="active" data-tab="detail">Resumen</button><button data-tab="logs">Logs</button><button data-tab="events">Eventos</button></div><div class="dialog-body"><div id="detailPane"></div><div id="logsPane" hidden><div class="log-options"><select id="container" aria-label="Contenedor"></select><label class="row"><input id="previous" type="checkbox">Anterior</label><button id="reloadLogs" title="Consultar logs" class="icon-button"><svg class="icon"><use href="#i-refresh"/></svg></button></div><p class="muted small" style="margin-bottom:12px">Hasta 200 líneas del contenedor seleccionado.</p><pre class="logs" id="logs"></pre></div><div id="eventsPane" hidden><div id="events"></div></div></div></dialog>
<script>
'use strict';
const $=id=>document.getElementById(id), el=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=String(text);return node};
let config=null,snapshot=null,activePod=null,attentionOnly=false,auto=true,busy=false,requestId=0,detailRequest=0,namespaceTimer;
const fmt=(value,digits=0)=>value==null?'—':new Intl.NumberFormat('es-CO',{maximumFractionDigits:digits}).format(value);
const query=data=>new URLSearchParams(data).toString();
const activeContext=()=> $('context').value==='@current'?(config?.current||''):$('context').value;
const selectedScope=()=>({context:activeContext(),namespace:$('namespace').value.trim()});
const sameScope=(a,b)=>a.context===b.context&&a.namespace===b.namespace;
async function api(path){const response=await fetch(path,{cache:'no-store'});const data=await response.json();if(!response.ok)throw Error(data.error||'No fue posible consultar Kubernetes.');return data}
function badge(text,kind=''){const n=el('span','state '+kind);n.append(el('span','dot'),document.createTextNode(text));return n}
function connection(text,kind=''){const n=$('connectionState');n.className='status-chip '+kind;n.replaceChildren(el('span','dot'),el('span','',text))}
function notify(text,error=false){$('notice').hidden=!text;$('notice').textContent=text;$('notice').className='notice'+(error?' error':'')}
function emptyRow(text){const tr=el('tr'),td=el('td','empty',text);td.colSpan=8;tr.append(td);$('rows').replaceChildren(tr)}
function clearView(text){snapshot=null;for(const id of ['total','ready','attention','restarts','cpu','memory','healthPercent','podCount'])$(id).textContent='—';$('healthRing').style.background='var(--line)';$('services').replaceChildren(el('span','muted small','Esperando una lectura del clúster.'));for(const id of ['cpuBar','memoryBar'])$(id).style.width='0';$('cpuCaption').textContent=$('memoryCaption').textContent='Sin métricas disponibles';$('updated').textContent='Sin lectura para este contexto';emptyRow(text)}
function filtered(){if(!snapshot)return[];const search=$('search').value.trim().toLowerCase(),micro=$('micro').value;return snapshot.pods.filter(p=>(!micro||p.micro===micro)&&(!attentionOnly||['danger','warning'].includes(p.severity))&&(!search||[p.name,p.micro,p.namespace,p.node,p.state].join(' ').toLowerCase().includes(search)))}
function resource(pods,key,limit,divisor,id){const measured=pods.filter(p=>p[key]!=null);$(id).textContent=measured.length?fmt(measured.reduce((n,p)=>n+p[key],0)/divisor,2):'—';const full=measured.length>0&&measured.every(p=>p[limit]>0);const capacity=full?measured.reduce((n,p)=>n+p[limit],0):0;const percent=capacity?measured.reduce((n,p)=>n+p[key],0)/capacity*100:null;$(id+'Caption').textContent=measured.length?`${measured.length}/${pods.length} pods con métricas`+(percent!=null?' · '+fmt(percent)+'% del límite':''):'Sin métricas disponibles';$(id+'Bar').style.width=percent==null?'0':Math.min(percent,100)+'%'}
function render(){if(!snapshot)return;const pods=filtered(),ready=pods.filter(p=>p.is_ready).length,issues=pods.filter(p=>['warning','danger'].includes(p.severity)).length;
$('total').textContent=pods.length;$('ready').textContent=ready;$('attention').textContent=issues;$('restarts').textContent=pods.reduce((n,p)=>n+p.restarts,0);$('scopeNote').textContent=new Set(pods.map(p=>p.micro)).size+' servicios · vista filtrada';$('attentionNote').textContent=issues?'Revisa los estados y eventos':'Sin alertas en esta lectura';$('podCount').textContent=pods.length;
const ratio=pods.length?ready/pods.length*100:0;$('healthPercent').textContent=pods.length?Math.round(ratio)+'%':'—';$('healthRing').style.background=`conic-gradient(var(--green) ${ratio}%, var(--line) 0)`;
const services=$('services');services.replaceChildren();const groups={};for(const p of pods){groups[p.micro]??={total:0,ready:0};groups[p.micro].total++;groups[p.micro].ready+=p.is_ready?1:0}for(const [name,s] of Object.entries(groups).slice(0,4)){const row=el('div','service-row'),title=el('div','row spread'),bar=el('div','bar'),fill=el('span');title.append(el('span','service-title',name),el('span','muted',s.ready+'/'+s.total));fill.style.width=(s.ready/s.total*100)+'%';bar.append(fill);row.append(title,bar);services.append(row)}if(!pods.length)services.append(el('span','muted small','Sin pods para estos filtros.'));if(Object.keys(groups).length>4)services.append(el('span','muted small','+'+(Object.keys(groups).length-4)+' servicios en la tabla'));
resource(pods,'cpu','cpu_limit',1000,'cpu');resource(pods,'memory','memory_limit',1024,'memory');
$('rows').replaceChildren();for(const p of pods){const tr=el('tr');const name=el('td'),button=el('button','pod-button',p.name);button.addEventListener('click',()=>openPod(p));name.append(button,el('div','pod-meta',p.micro+' · '+p.namespace));tr.append(name);const state=el('td');state.append(badge(p.state,p.severity));tr.append(state,el('td','',p.ready));for(const [key,lim,unit]of[['cpu','cpu_limit','m'],['memory','memory_limit',' MiB']]){const td=el('td','',p[key]==null?'—':fmt(p[key])+unit);if(p[key]!=null&&p[lim]>0){const bar=el('div','mini-bar'),fill=el('span');fill.style.width=Math.min(100,p[key]/p[lim]*100)+'%';bar.append(fill);td.append(bar)}tr.append(td)}tr.append(el('td','restart'+(p.restarts?' bad':''),p.restarts),el('td','muted',p.age));const go=el('td'),open=el('button','icon-button','›');open.setAttribute('aria-label','Ver '+p.name);open.style.border='0';open.addEventListener('click',()=>openPod(p));go.append(open);tr.append(go);$('rows').append(tr)}if(!pods.length)emptyRow('No se encontraron pods para estos filtros.');$('contextFoot').textContent=(snapshot.demo?'DEMO · ':'')+snapshot.context;
}
function microOptions(){const old=$('micro').value;$('micro').replaceChildren(new Option('Todos los servicios',''));for(const name of [...new Set(snapshot.pods.map(p=>p.micro))].sort())$('micro').append(new Option(name,name));if([...$('micro').options].some(o=>o.value===old))$('micro').value=old}
async function load(force=false){if(busy&&!force)return;const scope=selectedScope();if(!scope.context||!scope.namespace){clearView('Selecciona un contexto y un namespace.');return;}const id=++requestId;busy=true;$('reload').disabled=true;const previous=snapshot;if(previous&&!sameScope(previous,scope))clearView('Consultando el contexto seleccionado...');connection('Consultando');try{const data=await api('/api/pods?'+query(scope));if(id!==requestId||!sameScope(scope,selectedScope()))return;snapshot=data;microOptions();render();connection(data.demo?'Sesión de ejemplo':'Conectado',data.demo?'warning':'good');$('updated').textContent='Última lectura · '+new Date(data.updated).toLocaleTimeString('es-CO');notify(data.metrics_error?'Estado de pods disponible. CPU y memoria no disponibles: '+data.metrics_error:'');$('podPanel').classList.remove('stale')}catch(e){if(id!==requestId||!sameScope(scope,selectedScope()))return;connection('Revisar conexión','danger');notify((snapshot?'No se pudo actualizar; se conserva la lectura anterior. ':'')+e.message,true);if(snapshot){$('updated').textContent='DATOS SIN ACTUALIZAR · Última lectura: '+new Date(snapshot.updated).toLocaleTimeString('es-CO');$('podPanel').classList.add('stale')}else clearView('Conecta tu sesión de AWS para consultar los pods.')}finally{if(id===requestId){busy=false;$('reload').disabled=false}}}
function namespaceFor(context){try{return sessionStorage.getItem('namespace:'+context)||config.contexts.find(c=>c.name===context)?.namespace||'default'}catch{return config.contexts.find(c=>c.name===context)?.namespace||'default'}}
async function detect(initial=false){const previous=activeContext(),selection=$('context').value;try{const c=await api('/api/config');config=c;$('demoBanner').hidden=!c.demo;$('context').replaceChildren(new Option('Automático · '+(c.contexts.find(x=>x.name===c.current)?.label||'sin contexto'),'@current'));for(const item of c.contexts)$('context').append(new Option((item.eks?'EKS · ':'')+item.label,item.name));$('context').value=c.contexts.some(x=>x.name===selection)?selection:'@current';if(initial||previous!==activeContext()||!$('namespace').value)$('namespace').value=namespaceFor(activeContext());connectionInfo();if(c.error){connection('Configura tu sesión','warning');notify(c.error,true);clearView('Abre Conexión AWS para revisar la configuración.')}else if(!c.contexts.length){connection('Sin contexto','warning');notify('No hay contextos en kubeconfig. Ejecuta tu script habitual de conexión a EKS.');clearView('Esperando un contexto de Kubernetes.')}else if(initial||previous!==activeContext()||!snapshot)await load(true)}catch(e){connection('Sin conexión local','danger');notify(e.message,true)}}
function connectionInfo(){if(!config)return;const grid=$('connectionInfo');grid.replaceChildren();for(const[label,value]of[['kubectl',config.kubectl?'Disponible':'No encontrado en PATH'],['AWS CLI',config.aws?'Disponible':'No encontrada en PATH'],['Contextos',config.contexts.length+' detectados'],['Perfil heredado',config.profile||'Cadena de credenciales de kubectl']]){const tile=el('div','connection-tile');tile.append(el('strong','',label),el('p','',value));grid.append(tile)}const list=$('scriptList');list.replaceChildren();if(config.connected_script)list.append(el('p','instructions','Iniciado mediante: '+config.connected_script));for(const script of config.scripts){const box=el('div','script-item');box.append(el('strong','',script.name),el('p','',script.hints.join(' · ')),el('p','',script.path));list.append(box)}if(!config.scripts.length)list.append(el('p','instructions',config.demo?'La demo no inspecciona scripts ni consulta AWS.':'No encontramos candidatos. Coloca tu script habitual en la misma carpeta de pods_local.py.'));$('launchCommand').textContent=config.scripts.length===1?'py pods_local.py --connect-script auto':'py pods_local.py --connect-script "C:\\ruta\\tu-script.ps1"'}
function openConnection(){connectionInfo();$('awsCredentialContext').value=activeContext();$('connectionDialog').showModal()}
function openPod(p){activePod={...p,context:snapshot.context};$('podTitle').textContent=p.name;$('podSubtitle').textContent=p.namespace+' · '+snapshot.context;const detail=$('detailPane');detail.replaceChildren();const grid=el('dl','detail-grid');for(const[label,value]of[['Estado',p.state],['Contenedores listos',p.ready],['Nodo',p.node],['IP del pod',p.ip],['Creado',p.created?new Date(p.created).toLocaleString('es-CO'):'—'],['Reinicios',p.restarts]]){const group=el('div');group.append(el('dt','',label),el('dd','',value));grid.append(group)}detail.append(grid,el('h3','','Contenedores'));$('container').replaceChildren();for(const c of p.containers){$('container').append(new Option(c.name,c.name));const box=el('div','container-item'),head=el('div','row spread');head.append(el('strong','small',c.name),badge(c.ready?'Ready':'No listo',c.ready?'good':'warning'));box.append(head,el('p','',c.image));if(c.last_reason)box.append(el('p','','Última terminación: '+c.last_reason));box.append(el('pre','code',JSON.stringify(c.resources,null,2)));detail.append(box)}const failed=p.conditions.filter(c=>c.status==='False');if(failed.length){detail.append(el('h3','','Condiciones pendientes'));for(const c of failed)detail.append(el('p','instructions',c.type+' · '+(c.message||c.reason||'False')))}$('previous').checked=false;activateTab('detail');$('podDialog').showModal()}
function activateTab(name){document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));for(const tab of ['detail','logs','events'])$(tab+'Pane').hidden=tab!==name;if(name==='logs')loadLogs();if(name==='events')loadEvents()}
async function loadLogs(){if(!activePod)return;const id=++detailRequest,p=activePod;$('logs').textContent='Consultando logs...';try{const data=await api('/api/logs?'+query({context:p.context,namespace:p.namespace,pod:p.name,container:$('container').value,previous:$('previous').checked}));if(id===detailRequest)$('logs').textContent=data.text||'Sin líneas de logs.'}catch(e){if(id===detailRequest)$('logs').textContent=e.message}}
async function loadEvents(){if(!activePod)return;const id=++detailRequest,p=activePod;$('events').replaceChildren(el('p','muted small','Consultando eventos...'));try{const data=await api('/api/events?'+query({context:p.context,namespace:p.namespace,pod:p.name}));if(id!==detailRequest)return;$('events').replaceChildren();for(const e of data.events){const box=el('div','event');box.append(badge(e.type,e.type==='Warning'?'warning':'good'),el('strong','', ' '+e.reason),el('p','',e.message),el('small','',e.time+' · '+e.count+' ocurrencias'));$('events').append(box)}if(!data.events.length)$('events').append(el('p','muted small','No hay eventos disponibles para este pod.'))}catch(e){if(id===detailRequest)$('events').replaceChildren(el('p','instructions',e.message))}}
$('context').addEventListener('change',()=>{$('namespace').value=namespaceFor(activeContext());load(true)});$('namespace').addEventListener('input',()=>{clearTimeout(namespaceTimer);namespaceTimer=setTimeout(()=>{try{sessionStorage.setItem('namespace:'+activeContext(),$('namespace').value.trim())}catch{}load(true)},750)});$('namespace').addEventListener('keydown',e=>{if(e.key==='Enter'){clearTimeout(namespaceTimer);load(true)}});$('search').addEventListener('input',render);$('micro').addEventListener('change',render);$('reload').addEventListener('click',()=>load(true));$('allStates').onclick=()=>{attentionOnly=false;$('allStates').classList.add('active');$('problemStates').classList.remove('active');render()};$('problemStates').onclick=()=>{attentionOnly=true;$('problemStates').classList.add('active');$('allStates').classList.remove('active');render()};$('auto').onclick=()=>{auto=!auto;$('auto').textContent=auto?'● Actualización automática · 15 s':'Ⅱ Actualización pausada';if(auto)load()};$('theme').onclick=()=>{document.documentElement.classList.toggle('dark');try{localStorage.setItem('eks-theme',document.documentElement.classList.contains('dark')?'dark':'light')}catch{}};
$('connect').onclick=$('navConnection').onclick=openConnection;$('navOverview').onclick=()=>window.scrollTo({top:0,behavior:'smooth'});$('navPods').onclick=()=>{$('podPanel').scrollIntoView({behavior:'smooth'});$('search').focus({preventScroll:true})};$('detect').onclick=async()=>{await detect();await load(true)};document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>$(b.dataset.close).close());document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>activateTab(b.dataset.tab));$('container').onchange=$('previous').onchange=$('reloadLogs').onclick=loadLogs;$('podDialog').addEventListener('close',()=>{detailRequest++;activePod=null});
try{if(localStorage.getItem('eks-theme')==='dark')document.documentElement.classList.add('dark')}catch{}setInterval(()=>{if(auto&&!document.hidden&&currentView==='eks')load()},15000);setInterval(()=>{if(!document.hidden&&!busy&&currentView==='eks')detect()},30000);detect(true);
const LOCAL_TOKEN='__LOCAL_TOKEN__';
let currentView='eks',perfData=null,perfBusy=false,lastAutoKey='',azureDefinition=null,azureList=[],jtlText='';
async function post(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':LOCAL_TOKEN},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error||'No se pudo completar la operación local.');return data}
function sectionNotice(id,text,error=false){$(id).hidden=!text;$(id).textContent=text;$(id).className='notice'+(error?' error':'')}
function azureSelection(){const kind=$('azKind').value;return {kind,definition_id:$('azId').value.trim(),stage:kind==='release'?$('azEnvironment').value:$('azStage').value.trim()}}
function showView(view){currentView=view;for(const name of ['eks','performance','inventory','azure'])$(name+'View').hidden=name!==view;const ids={eks:'navOverview',performance:'navPerformance',inventory:'navInventory',azure:'navAzure'};document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('active',n.id===ids[view]));if(view==='performance'){refreshPerformance();if(!perfData?.scope&&activeContext())setupPerformance('live')}if(view==='azure'&&config?.demo&&!azureList.length)loadAzureDefinitions();}
async function setupPerformance(mode){if(perfBusy)return;const target=selectedScope();if(!target.context||!target.namespace){sectionNotice('perfNotice','Selecciona contexto y namespace.',true);return}if(perfData&&(perfData.mode==='manual'||perfData.run?.active)&&mode!=='live'){sectionNotice('perfNotice','Finaliza la captura actual antes de iniciar una nueva.',true);showView('performance');return}if(mode==='auto'&&!azureSelection().definition_id){showView('azure');sectionNotice('azureNotice','Conecta Azure y elige el pipeline y ambiente que corresponden a este ámbito EKS.');return}perfBusy=true;try{perfData=await post('/api/performance/config',{...target,micro:$('micro').value,mode,selection:azureSelection()});jtlText='';renderPerformance();showView('performance')}catch(e){sectionNotice(currentView==='azure'?'azureNotice':'perfNotice',e.message,true)}finally{perfBusy=false}}
async function refreshPerformance(){try{perfData=await api('/api/performance');renderPerformance();if(perfData.mode==='auto'&&perfData.run?.active&&lastAutoKey!==perfData.run.key){lastAutoKey=perfData.run.key;showView('performance');}}catch(e){sectionNotice('perfNotice',e.message,true)}}
function svgEl(tag,attrs,text){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const[k,v]of Object.entries(attrs||{}))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;return e}
function drawChart(id,points,series,divisor=1,unit=''){const root=$(id);root.replaceChildren();if(!points.length){root.append(el('div','chart-empty','Las gráficas se construirán con las lecturas de esta sesión.'));return}const width=520,height=185,left=48,right=12,top=12,bottom=30,w=width-left-right,h=height-top-bottom;let max=0;for(const p of points)for(const s of series)if(p[s.key]!=null)max=Math.max(max,p[s.key]/divisor);max=max?max*1.12:1;const first=points[0].time,last=Math.max(points[points.length-1].time,first+15);const x=t=>left+(t-first)/(last-first)*w,y=v=>top+h-v/max*h;const svg=svgEl('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':id+' · '+unit});for(let i=0;i<4;i++){const value=max*i/3;svg.append(svgEl('line',{x1:left,y1:y(value),x2:width-right,y2:y(value),stroke:'var(--line)','stroke-width':1}));svg.append(svgEl('text',{x:left-7,y:y(value)+4,'text-anchor':'end',fill:'var(--muted)','font-size':10},fmt(value,1)))}svg.append(svgEl('text',{x:left,y:height-4,fill:'var(--muted)','font-size':9},new Date(first*1000).toLocaleTimeString('es-CO')));svg.append(svgEl('text',{x:width-right,y:height-4,'text-anchor':'end',fill:'var(--muted)','font-size':9},new Date(points[points.length-1].time*1000).toLocaleTimeString('es-CO')));for(const s of series){let d='',start=true,priorTime=null;for(const p of points){if(priorTime!==null&&p.time-priorTime>45)start=true;priorTime=p.time;if(p[s.key]==null){start=true;continue}d+=(start?'M':'L')+x(p.time).toFixed(1)+','+y(p[s.key]/divisor).toFixed(1)+' ';start=false}if(d)svg.append(svgEl('path',{d,fill:'none',stroke:s.color,'stroke-width':2.4,'stroke-dasharray':s.dash||'','stroke-linejoin':'round'}));if(points.length===1&&points[0][s.key]!=null)svg.append(svgEl('circle',{cx:x(first),cy:y(points[0][s.key]/divisor),r:3,fill:s.color}));}root.append(svg);const legend=el('div','chart-legend');for(const s of series){const label=el('span','',s.label);label.style.setProperty('--legend-color',s.color);legend.append(label)}root.append(legend)}
function renderPerformance(){if(!perfData)return;const d=perfData,latest=d.latest,samples=d.samples||[],sample=latest?.sample;let state=d.mode==='manual'?'Capturando':d.mode==='auto'?(d.run?.active?'Performance activo':d.ended?'Captura finalizada · en espera':'Esperando ejecución'):d.ended?'Captura finalizada':d.scope?'Observación en vivo':'Sin captura';$('perfState').textContent=state;$('perfState').className='status-chip '+(d.mode==='manual'||d.run?.active?'good':'');$('captureScope').textContent=d.scope?`${d.scope.context} · ${d.scope.namespace} · ${d.scope.micro||'Todos los servicios'}`:'Selecciona un ambiente de EKS.';$('startCapture').disabled=d.mode==='manual'||!!d.run?.active;$('stopCapture').disabled=!d.scope;
const issues=[d.error,d.azure_error,latest?.metrics_error].filter(Boolean);sectionNotice('perfNotice',issues.join(' · '),!!d.error);sectionNotice('runInfo',d.run?'Ejecución: '+d.run.name+' · '+d.run.status+(d.run.result?' · '+d.run.result:''):'');$('perfCpu').textContent=sample?.cpu!=null?fmt(sample.cpu/1000,2)+' cores':'—';$('perfMem').textContent=sample?.memory!=null?fmt(sample.memory/1024,2)+' GiB':'—';$('perfCpuMeta').textContent='Request '+fmt(sample?.cpu_request==null?null:sample.cpu_request/1000,2)+' · Limit '+fmt(sample?.cpu_limit==null?null:sample.cpu_limit/1000,2)+' cores';$('perfMemMeta').textContent='Request '+fmt(sample?.memory_request==null?null:sample.memory_request/1024,2)+' · Limit '+fmt(sample?.memory_limit==null?null:sample.memory_limit/1024,2)+' GiB';$('perfReady').textContent=sample?sample.ready+' / '+sample.total:'—';$('perfMeasured').textContent=sample?sample.measured+'/'+sample.total+' pods con CPU y memoria':'Sin métricas';$('perfSamples').textContent=samples.length;$('perfDuration').textContent=samples.length?fmt((samples[samples.length-1].time-samples[0].time)/60,1)+' min observados · cada 15 s':'Muestreo cada 15 s';
const cpuSeries=[{key:'cpu',label:'Uso observado',color:'#159b77'},{key:'cpu_request',label:'Requests',color:'#d5a700',dash:'5 4'},{key:'cpu_limit',label:'Limits',color:'#6983ac',dash:'3 4'}];drawChart('cpuChart',samples,cpuSeries,1000,'cores');drawChart('memChart',samples,cpuSeries.map(s=>({...s,key:s.key.replace('cpu','memory')})),1024,'GiB');drawChart('podsChart',samples,[{key:'total',label:'Pods observados',color:'#d5a700'},{key:'ready',label:'Ready',color:'#159b77'}],1,'pods');
$('hpaList').replaceChildren();const hpaAccess=latest?.access?.hpa;if(hpaAccess&&hpaAccess.status!=='available')$('hpaList').append(el('p','instructions','HPA: '+(hpaAccess.status==='forbidden'?'sin permiso de lectura':hpaAccess.message)));else if(latest&&!(latest.hpas||[]).length)$('hpaList').append(el('p','instructions','Sin HPA asociado por nombre en este ámbito. La etiqueta del microservicio puede diferir del Deployment; consulta todos los servicios para revisar.'));for(const h of latest?.hpas||[]){const box=el('div','hpa-item'),head=el('div','row spread');head.append(el('strong','',h.name),badge(`${h.current??'—'} / ${h.max??'—'}`,h.current>=h.max?'warning':'good'));const target=(h.targets||[]).map(t=>{const v=t.resource||t.containerResource||{};return v.name?`${v.name}: ${v.target?.averageUtilization!=null?v.target.averageUtilization+'% del request':v.target?.averageValue||'métrica'}`:t.type}).join(' · ');box.append(head,el('p','',`Mínimo ${h.min} · deseadas ${h.desired??'—'} · máximo ${h.max} · ${target}`));const bar=el('div','bar'),fill=el('span');fill.style.width=(h.max?Math.min(100,(h.current||0)/h.max*100):0)+'%';bar.append(fill);box.append(bar);$('hpaList').append(box)}
const rows=$('resourcesRows');rows.replaceChildren();for(const p of latest?.pods||[]){const tr=el('tr');tr.append(el('td','',p.name));for(const key of ['cpu','cpu_request','cpu_limit','memory','memory_request','memory_limit'])tr.append(el('td','',p[key]==null?'—':fmt(p[key])+(key.startsWith('cpu')?'m':' MiB')));rows.append(tr)}if(!latest){const tr=el('tr'),td=el('td','empty','Esperando la primera lectura...');td.colSpan=7;tr.append(td);rows.append(tr)}
$('findings').replaceChildren();for(const f of d.analysis||[]){const box=el('article','finding '+f.level);box.append(el('strong','',f.title),el('p','',f.evidence),el('p','action',f.action));$('findings').append(box)}$('coverageNote').textContent=d.coverage_note+' Las métricas faltantes se indican; el uso agregado puede ser parcial. Exporta el análisis antes de cerrar.';
$('jtlStats').hidden=!d.jtl;$('latencyChart').hidden=!d.jtl;if(d.jtl){const j=d.jtl;$('jtlStats').replaceChildren();for(const[label,value,note]of[['Muestras',fmt(j.samples),'Filas del archivo'],['P95',fmt(j.p95)+' ms','95% de las muestras'],['P99',fmt(j.p99)+' ms','99% de las muestras'],['Errores',fmt(j.error_percent,2)+'%',fmt(j.errors)+' muestras fallidas'],['Throughput',fmt(j.throughput,2)+'/s','Muestras por segundo']]){const card=el('div','jtl-stat');card.append(el('span','',label),el('strong','',value),el('span','',note));$('jtlStats').append(card)}$('jtlMessage').textContent=j.note;drawChart('latencyChart',j.series,[{key:'p95',label:'P95 por intervalo',color:'#6983ac'}],1,'ms')}
}
async function loadInventory(){const target=selectedScope();$('inventoryGrid').replaceChildren(el('p','muted','Consultando recursos accesibles...'));$('loadInventory').disabled=true;try{const data=await api('/api/inventory?'+query(target));$('inventoryGrid').replaceChildren();for(const r of data.resources){const card=el('article','inventory-card'),head=el('div','row spread');head.append(el('h3','',r.kind),badge(r.status==='available'?'Disponible':r.status==='forbidden'?'Sin permiso':'No disponible',r.status==='available'?'good':'warning'));card.append(head,el('strong','',r.count==null?'—':r.count));if(r.message)card.append(el('p','',r.message));if(r.names?.length)card.append(el('p','',r.names.join(' · ')));if(r.detail?.length){const details=el('details'),summary=el('summary','','Ver configuración');details.append(summary,el('pre','',JSON.stringify(r.detail,null,2)));card.append(details)}$('inventoryGrid').append(card)}sectionNotice('inventoryNotice',`${data.context||target.context} · ${data.namespace||target.namespace}`)}catch(e){sectionNotice('inventoryNotice',e.message,true)}finally{$('loadInventory').disabled=false}}
async function loadAzureDefinitions(){try{const data=await api('/api/azure/definitions');azureList=data.definitions||[];$('azDefinitions').replaceChildren(new Option('Seleccionar pipeline',''));for(const d of azureList)$('azDefinitions').append(new Option(d.kind+' · '+d.name,d.kind+':'+d.id));$('azureState').textContent=data.connected?(config?.demo?'Demo · datos simulados':'Conectado'):'Sin conexión';sectionNotice('azureNotice',(data.warnings||[]).join(' · '));}catch(e){sectionNotice('azureNotice',e.message,true)}}
async function connectAzure(e){e.preventDefault();$('azConnect').disabled=true;try{const authInfo=await post('/api/azure/connect',{organization:$('azOrg').value,project:$('azProject').value,auth:$('azAuth').value,token:$('azToken').value,block:$('azCredentialBlock').value});if(authInfo.organization)$('azOrg').value=authInfo.organization;if(authInfo.project)$('azProject').value=authInfo.project;if(authInfo.auth==='pat'){$('azAuth').value='pat';$('azAuth').dispatchEvent(new Event('change'))}$('azToken').value='';await loadAzureDefinitions();try{sessionStorage.setItem('az-project',JSON.stringify({org:$('azOrg').value,project:$('azProject').value}))}catch{}}catch(e){sectionNotice('azureNotice',e.message,true)}finally{$('azToken').value='';$('azCredentialBlock').value='';$('azConnect').disabled=false}}
async function readDefinition(){const {kind,definition_id}=azureSelection();if(!definition_id){sectionNotice('azureNotice','Escribe el ID de definición o selecciona un pipeline.');return}$('azReadDefinition').disabled=true;try{azureDefinition=await api('/api/azure/definition?'+query({kind,id:definition_id}));$('azEnvironment').replaceChildren(new Option('Selecciona el ambiente',''));for(const env of azureDefinition.environments)$('azEnvironment').append(new Option(env.name,env.id));renderVariables();sectionNotice('azureNotice',(azureDefinition.warnings||[]).join(' · '));}catch(e){sectionNotice('azureNotice',e.message,true)}finally{$('azReadDefinition').disabled=false}}
function renderVariables(){const filter=$('variableSearch').value.toLowerCase();const rows=(azureDefinition?.variables||[]).filter(v=>(v.name+' '+v.origin).toLowerCase().includes(filter));$('variableCount').textContent=rows.length;$('variablesRows').replaceChildren();for(const v of rows){const tr=el('tr');tr.append(el('td','',v.name),el('td','var-value',v.value),el('td','muted',v.origin));$('variablesRows').append(tr)}}
async function importJtl(){if(!jtlText){sectionNotice('perfNotice','Selecciona un archivo JTL/CSV primero.');return}try{perfData=await post('/api/performance/jtl',{csv:jtlText,label:$('jtlLabel').value,p95_ms:Number($('sloP95').value),errors_percent:Number($('sloErrors').value)});renderPerformance()}catch(e){sectionNotice('perfNotice',e.message,true)}}
function downloadReport(){if(!perfData?.samples?.length&&!perfData?.jtl){sectionNotice('perfNotice','Primero captura métricas o importa los resultados.');return}const report={product:'Bancolombia EKS Console · propuesta local',exported_at:new Date().toISOString(),...perfData,azure_definition:azureDefinition};const blob=new Blob([JSON.stringify(report,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download='performance-'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function environments(){try{return JSON.parse(localStorage.getItem('eks-environments')||'[]')}catch{return[]}}
function renderEnvironments(){const list=environments(),old=$('environment').value;$('environment').replaceChildren(new Option('Sesión actual / personalizado',''));$('savedEnvironments').replaceChildren();for(const [i,v]of list.entries()){$('environment').append(new Option(v.name,String(i)));const row=el('div','script-item'),head=el('div','row spread'),del=el('button','small','Eliminar');del.onclick=()=>{const all=environments();all.splice(i,1);localStorage.setItem('eks-environments',JSON.stringify(all));renderEnvironments()};head.append(el('strong','',v.name),del);row.append(head,el('p','',v.context+' · '+v.namespace));$('savedEnvironments').append(row)}if([...$('environment').options].some(o=>o.value===old))$('environment').value=old}
function openEnvironments(){$('envContext').value=activeContext();$('envNamespace').value=$('namespace').value;renderEnvironments();$('environmentDialog').showModal()}
$('saveEnvironment').onclick=()=>{const v={name:$('envName').value.trim(),context:$('envContext').value.trim(),namespace:$('envNamespace').value.trim()};if(!v.name||!v.context||!v.namespace){$('envName').reportValidity();return}const list=environments(),index=list.findIndex(e=>e.name===v.name);if(index>=0)list[index]=v;else list.push(v);localStorage.setItem('eks-environments',JSON.stringify(list));renderEnvironments();$('environmentDialog').close()};$('environment').onchange=()=>{if($('environment').value==='')return;const v=environments()[Number($('environment').value)];if(!v)return;if(![...$('context').options].some(o=>o.value===v.context))$('context').append(new Option(v.context,v.context));$('context').value=v.context;$('namespace').value=v.namespace;load(true);};
$('navOverview').onclick=()=>showView('eks');$('navPods').onclick=()=>{showView('eks');$('podPanel').scrollIntoView({behavior:'smooth'})};$('navPerformance').onclick=()=>showView('performance');$('navInventory').onclick=()=>showView('inventory');$('navAzure').onclick=()=>showView('azure');$('manageEnvironments').onclick=openEnvironments;document.querySelectorAll('[data-view]').forEach(b=>b.onclick=()=>showView(b.dataset.view));$('startCapture').onclick=()=>setupPerformance('manual');$('armCapture').onclick=$('azureArm').onclick=()=>setupPerformance('auto');$('stopCapture').onclick=async()=>{try{perfData=await post('/api/performance/finish',{});renderPerformance()}catch(e){sectionNotice('perfNotice',e.message,true)}};$('exportPerf').onclick=downloadReport;$('loadInventory').onclick=loadInventory;$('azureForm').onsubmit=connectAzure;$('azAuth').onchange=()=>{$('patField').hidden=$('azAuth').value!=='pat';$('authHelp').textContent=$('azAuth').value==='pat'?'Introduce un PAT de lectura permitido por tu organización.':'Ejecuta az login en tu terminal para usar Microsoft Entra.'};$('azDisconnect').onclick=async()=>{await post('/api/azure/disconnect',{});azureDefinition=null;azureList=[];$('azureState').textContent='Sin conexión';$('azDefinitions').replaceChildren(new Option('Sin conexión',''));renderVariables()};$('azDefinitions').onchange=()=>{const v=$('azDefinitions').value;if(!v)return;const[kind,id]=v.split(':');$('azKind').value=kind;$('azId').value=id;readDefinition()};$('azReadDefinition').onclick=readDefinition;$('variableSearch').oninput=renderVariables;$('jtlFile').onchange=async()=>{const file=$('jtlFile').files[0];if(!file)return;if(file.size>10_000_000){sectionNotice('perfNotice','El archivo supera 10 MB.',true);return}jtlText=await file.text();await importJtl()};$('reprocessJtl').onclick=importJtl;
try{const p=JSON.parse(sessionStorage.getItem('az-project')||'null');if(p){$('azOrg').value=p.org;$('azProject').value=p.project}}catch{}renderEnvironments();setInterval(()=>{if(currentView==='performance'||perfData?.mode==='auto'||perfData?.mode==='manual')refreshPerformance()},5000);refreshPerformance();


$('applyAwsBlock').onclick=async()=>{const button=$('applyAwsBlock');button.disabled=true;$('awsCredentialMessage').textContent='Aplicando sesión al contexto...';try{const result=await post('/api/aws/session',{context:$('awsCredentialContext').value,block:$('awsCredentialBlock').value});$('awsCredentialBlock').value='';$('awsCredentialMessage').textContent=result.demo?'Demo: bloque validado, sin usar las credenciales.':'Bloque cargado en memoria. Las siguientes consultas de este contexto usarán esta sesión.';await load(true)}catch(e){$('awsCredentialMessage').textContent=e.message}finally{button.disabled=false}};
$('removeAwsBlock').onclick=async()=>{try{await post('/api/aws/disconnect',{context:$('awsCredentialContext').value});$('awsCredentialBlock').value='';$('awsCredentialMessage').textContent='Sesión pegada eliminada. Se vuelve a usar tu conexión habitual.'}catch(e){$('awsCredentialMessage').textContent=e.message}};
$('connectionDialog').addEventListener('close',()=>{$('awsCredentialBlock').value=''});

</script></body></html>


'''

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.headers.get('Host') not in allowed:
            return self.respond(403, b'Host no permitido', 'text/plain')
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + host for host in allowed}:
            return self.respond(403, b'Origen no permitido', 'text/plain')
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query)
        param = lambda key, default='': params.get(key, [default])[0]
        if parsed.path == '/': return self.respond(200, HTML.replace('__LOCAL_TOKEN__',LOCAL_TOKEN).encode(), 'text/html; charset=utf-8')
        try:
            if parsed.path == '/api/config': result = config_info()
            elif parsed.path == '/api/performance': result=PERF.status()
            elif parsed.path == '/api/azure/definitions': result=azure_definitions()
            elif parsed.path == '/api/azure/definition': result=azure_definition(param('kind'),param('id'))
            elif parsed.path == '/api/inventory': result=inventory(param('namespace'),param('context'))
            elif parsed.path == '/api/pods': result = list_pods(param('namespace', 'default'), param('context'))
            elif parsed.path in ('/api/logs', '/api/events'):
                result = pod_content(parsed.path.rsplit('/', 1)[1], param('namespace'), param('pod'),
                    param('context'), param('container'), param('previous') == 'true')
            else: return self.respond(404, b'No encontrado', 'text/plain')
            self.respond(200, json.dumps(result, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
        except (RuntimeError, ValueError, OSError) as exc:
            self.respond(400, json.dumps({'error': str(exc)}, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_POST(self):
        allowed={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        origin=self.headers.get('Origin')
        if self.headers.get('Host') not in allowed or (origin and origin not in {'http://'+h for h in allowed}) or self.headers.get('X-Local-Token')!=LOCAL_TOKEN:
            return self.respond(403,b'{"error":"Solicitud local no autorizada."}','application/json')
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=12_000_000: raise ValueError('Solicitud vacía o demasiado grande.')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Solicitud inválida.')
            path=urlsplit(self.path).path
            if path=='/api/performance/config': result=PERF.configure(data)
            elif path=='/api/performance/finish':result=PERF.finish()
            elif path=='/api/performance/jtl':result=PERF.import_results(data)
            elif path=='/api/aws/session':result=aws_block_connect(data)
            elif path=='/api/aws/disconnect':
                with AWS_SESSION_LOCK:AWS_SESSIONS.pop(str(data.get('context','')),None)
                result={'removed':True}
            elif path=='/api/azure/connect': result=azure_definitions() if DEMO else azure_block_connect(data)
            elif path=='/api/azure/disconnect':
                AZURE.disconnect()
                if PERF.mode=='auto':PERF.finish()
                result={'connected':False}
            else:return self.respond(404,b'{"error":"Ruta no encontrada."}','application/json')
            self.respond(200,json.dumps(result,ensure_ascii=False).encode(),'application/json; charset=utf-8')
        except (ValueError,RuntimeError,OSError) as exc:
            self.respond(400,json.dumps({'error':str(exc)},ensure_ascii=False).encode(),'application/json; charset=utf-8')

    def respond(self, status, body, kind):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass

    def log_message(self, *args): pass


def connect_and_launch(script, args):
    if script == 'auto':
        candidates = discover_scripts()
        if len(candidates) != 1:
            choices = '\n'.join(c['path'] for c in candidates) or 'No se encontraron scripts junto al tablero.'
            raise ValueError('Para elegir el script usa --connect-script RUTA.\n' + choices)
        script = candidates[0]['path']
    path = Path(script).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in SCRIPT_TYPES:
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
        if any(ch in str(path) + sys.executable + str(BASE) for ch in '%!^&|<>\r\n"'):
            raise ValueError('Para BAT/CMD usa rutas sin caracteres especiales de la consola.')
        command = ['cmd.exe', '/d', '/v:off', '/s', '/c',
            'call "%EKS_CONSOLE_SCRIPT%" && "%EKS_CONSOLE_PYTHON%" "%EKS_CONSOLE_APP%" '
            '--port "%EKS_CONSOLE_PORT%" --connected-script "%EKS_CONSOLE_SCRIPT%"' + extra]
    print(f'Conectando mediante {path.name}. Completa el inicio de sesión en esta terminal.', flush=True)
    return subprocess.call(command, env=env, cwd=str(path.parent))


def main():
    global DEMO, CONNECTED_SCRIPT, PERF
    parser = argparse.ArgumentParser(description='Bancolombia | EKS Console local. Python + kubectl.')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--demo', action='store_true', help='Mostrar datos simulados para revisar el diseño')
    parser.add_argument('--connect-script', help='Ejecutar tu script y heredar su sesión; RUTA o auto')
    parser.add_argument('--connected-script', default='', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.connect_script and args.demo: parser.error('Usa --demo o --connect-script por separado.')
    if args.connect_script:
        try: return connect_and_launch(args.connect_script, args)
        except (ValueError, OSError) as exc: parser.error(str(exc))
    DEMO, CONNECTED_SCRIPT = args.demo, args.connected_script
    try: server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    except OSError as exc: parser.error(f'No se puede abrir el puerto {args.port}: {exc}')
    PERF=PerformanceMonitor()
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Bancolombia | EKS Console\n{url}\n' + ('DEMO: datos simulados.\n' if DEMO else '') + 'Ctrl+C para cerrar.', flush=True)
    if not args.no_browser:
        timer = threading.Timer(.6, lambda: webbrowser.open(url)); timer.daemon = True; timer.start()
    try: server.serve_forever()
    except KeyboardInterrupt: print('\nTablero detenido.')
    finally:
        PERF.close();AZURE.disconnect();AWS_SESSIONS.clear();server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
