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
import configparser
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .aws_auth import AWS_KEYS, AwsSessionManager, parse_environment_block
from .kubernetes_models import demo_snapshot, number, pod_record
from .performance import PerformanceMonitor as BasePerformanceMonitor

BASE = Path(__file__).resolve().parent.parent
NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
SCRIPT_TYPES = {'.ps1', '.sh', '.bat', '.cmd'}
DEMO = False
CONNECTED_SCRIPT = ''
KUBE_CONFIG_LOCK = threading.RLock()
KUBE_CONFIG_CACHE = {'data': None, 'expires': 0.0}
LOCAL_DETAILS_LOCK = threading.RLock()
LOCAL_DETAILS_CACHE = {'data': None, 'expires': 0.0}


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


def scope(namespace):
    namespace = str(namespace or '').strip()
    if namespace == '*':
        raise ValueError('Por seguridad, selecciona un namespace exacto. Las consultas globales están deshabilitadas.')
    if not NAME.fullmatch(namespace) or '.' in namespace or len(namespace) > 63:
        raise ValueError('Escribe un namespace válido y específico.')
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


def kube_config(force=False):
    now = time.monotonic()
    with KUBE_CONFIG_LOCK:
        if not force and KUBE_CONFIG_CACHE['data'] is not None and KUBE_CONFIG_CACHE['expires'] > now:
            return KUBE_CONFIG_CACHE['data']
        data = json.loads(kubectl('config', 'view', '-o', 'json', timeout=4))
        KUBE_CONFIG_CACHE.update(data=data, expires=now + 10)
        return data


def local_details(force=False):
    now = time.monotonic()
    with LOCAL_DETAILS_LOCK:
        if not force and LOCAL_DETAILS_CACHE['data'] is not None and LOCAL_DETAILS_CACHE['expires'] > now:
            return LOCAL_DETAILS_CACHE['data']
        data = {'profiles': profile_names(), 'scripts': discover_scripts()}
        LOCAL_DETAILS_CACHE.update(data=data, expires=now + 60)
        return data


def config_info(force=False, details=False):
    if DEMO:
        return {'contexts': [{'name': 'demo-eks-qa', 'label': 'eks-documentos-qa', 'namespace': 'generaciondocumentaldigital-qa', 'eks': True, 'region': 'us-east-1'}],
                'current': 'demo-eks-qa', 'profiles': ['demo-qa'], 'profile': 'demo-qa',
                'scripts': [], 'connected_script': '', 'demo': True, 'error': None,
                'kubectl': True, 'aws': True, 'temporary_contexts': []}
    extras = local_details(force) if details else {'profiles': [], 'scripts': []}
    info = {'contexts': [], 'current': '', 'profiles': extras['profiles'],
            'profile': os.environ.get('AWS_PROFILE') or os.environ.get('AWS_DEFAULT_PROFILE') or '',
            'scripts': extras['scripts'], 'connected_script': Path(CONNECTED_SCRIPT).name if CONNECTED_SCRIPT else '',
            'demo': False, 'kubectl': bool(shutil.which('kubectl')), 'aws': bool(shutil.which('aws')), 'error': None}
    with AWS_SESSION_LOCK:
        info['temporary_contexts'] = sorted(AWS_SESSIONS)
    try:
        cfg = kube_config(force)
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
    cfg = kube_config()
    selected = context or cfg.get('current-context', '')
    if not selected or selected not in {c.get('name') for c in cfg.get('contexts') or []}:
        raise ValueError('No hay un contexto válido. Ejecuta tu conexión a EKS y vuelve a detectar.')
    return selected


def metrics_for(namespace, context):
    try:
        raw = kubectl('--request-timeout=5s', 'top', 'pods', *scope(namespace), '--no-headers', context=context, timeout=7)
        data = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) < 3: continue
            ns, (name, cpu, mem) = namespace, parts[:3]
            data[(ns, name)] = {'cpu': number(cpu, True), 'memory': number(mem)}
        return data, None
    except RuntimeError as exc:
        return {}, str(exc)


def list_pods(namespace, context, include_metrics=True):
    started = time.perf_counter()
    scope(namespace)
    if DEMO:
        result = demo_snapshot(namespace, context)
        result['query_ms'] = round((time.perf_counter() - started) * 1000)
        return result
    context = resolve_context(context)
    if include_metrics:
        with ThreadPoolExecutor(max_workers=2) as pool:
            metrics_task = pool.submit(metrics_for, namespace, context)
            raw = json.loads(kubectl('--request-timeout=8s', 'get', 'pods', *scope(namespace), '-o', 'json', context=context, timeout=10))
            metrics, error = metrics_task.result()
    else:
        raw = json.loads(kubectl('--request-timeout=8s', 'get', 'pods', *scope(namespace), '-o', 'json', context=context, timeout=10))
        metrics, error = {}, None
    pods = [pod_record(p, metrics.get((p['metadata'].get('namespace'), p['metadata'].get('name')), {}))
            for p in raw.get('items') or []]
    pods.sort(key=lambda p: ({'danger': 0, 'warning': 1, 'good': 2, 'neutral': 3}[p['severity']], p['name']))
    return {'pods': pods, 'context': context, 'namespace': namespace, 'metrics_error': error,
            'updated': datetime.now().astimezone().isoformat(timespec='seconds'), 'demo': False,
            'query_ms': round((time.perf_counter() - started) * 1000)}


def pod_metrics(namespace, context):
    started = time.perf_counter()
    scope(namespace)
    if DEMO:
        snapshot = demo_snapshot(namespace, context)
        values = [{'namespace': p['namespace'], 'name': p['name'], 'cpu': p['cpu'], 'memory': p['memory']}
                  for p in snapshot['pods']]
        return {'metrics': values, 'error': None, 'query_ms': round((time.perf_counter() - started) * 1000)}
    context = resolve_context(context)
    values, error = metrics_for(namespace, context)
    return {'metrics': [{'namespace': ns, 'name': name, **metric} for (ns, name), metric in values.items()],
            'error': error, 'query_ms': round((time.perf_counter() - started) * 1000)}


def pod_content(kind, namespace, pod, context, container='', previous=False, since=''):
    scope(namespace); validate_name(pod)
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


class PerformanceMonitor(BasePerformanceMonitor):
    """Fachada compatible con el servidor y el lanzador actuales."""

    def __init__(self):
        super().__init__(scope, capacity_snapshot, AZURE, lambda: DEMO)

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

AWS = AwsSessionManager()
AWS_SESSIONS = AWS.sessions
AWS_SESSION_LOCK = AWS.lock
AZURE_KEYS={'AZURE_DEVOPS_ORG_URL','AZURE_DEVOPS_PROJECT','AZURE_DEVOPS_EXT_PAT'}


def aws_process_env(context):
    return AWS.process_env(context)


def aws_connection_status(context):
    return AWS.status(
        context,
        demo=DEMO,
        resolve_context=resolve_context,
        kubectl=kubectl,
        run_command=run_command,
    )


def aws_block_connect(data):
    return AWS.connect(
        data,
        demo=DEMO,
        resolve_context=resolve_context,
        kubectl=kubectl,
        run_command=run_command,
    )


def azure_block_connect(data):
    block=str(data.get('block','')).strip()
    if not block:return AZURE.configure(data)
    values=parse_environment_block(block,AZURE_KEYS)
    return AZURE.configure({'organization':values.get('AZURE_DEVOPS_ORG_URL') or data.get('organization'),
                            'project':values.get('AZURE_DEVOPS_PROJECT') or data.get('project'),
                            'auth':'pat','token':values.get('AZURE_DEVOPS_EXT_PAT','')})
