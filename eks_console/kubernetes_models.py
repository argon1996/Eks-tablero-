"""Transformación de respuestas Kubernetes en modelos de la interfaz."""

import math
import re
import time
from datetime import datetime, timezone


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
    multiplier = {
        '': 1, 'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4,
        'K': 1000, 'k': 1000, 'm': .001, 'M': 1000**2, 'G': 1000**3, 'T': 1000**4,
    }.get(unit)
    return value * multiplier / 1024**2 if multiplier else None


def age(iso):
    if not iso:
        return '—'
    try:
        elapsed = max(0, int((
            datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace('Z', '+00:00'))
        ).total_seconds()))
        if elapsed < 60:
            return f'{elapsed}s'
        if elapsed < 3600:
            return f'{elapsed // 60}m'
        if elapsed < 86400:
            return f'{elapsed // 3600}h'
        return f'{elapsed // 86400}d'
    except (TypeError, ValueError):
        return '—'


def pod_state(item):
    meta, status, spec = item.get('metadata') or {}, item.get('status') or {}, item.get('spec') or {}
    phase = status.get('phase') or 'Unknown'
    if meta.get('deletionTimestamp'):
        return 'Terminating', 'warning', False
    ready = any(
        condition.get('type') == 'Ready' and condition.get('status') == 'True'
        for condition in status.get('conditions') or []
    )
    init_spec = {container['name']: container for container in spec.get('initContainers') or []}
    for container in status.get('initContainerStatuses') or []:
        state = container.get('state') or {}
        if state.get('waiting', {}).get('reason'):
            return 'Init:' + state['waiting']['reason'], 'danger', False
        if state.get('terminated', {}).get('exitCode', 0) != 0:
            return 'Init:' + state['terminated'].get('reason', 'Error'), 'danger', False
        if 'running' in state and init_spec.get(container.get('name'), {}).get('restartPolicy') != 'Always':
            return 'Initializing', 'warning', False
    if phase == 'Succeeded':
        return 'Completed', 'neutral', False
    if phase == 'Failed':
        return status.get('reason') or 'Failed', 'danger', False
    waiting = [
        (container.get('state') or {}).get('waiting') or {}
        for container in status.get('containerStatuses') or []
    ]
    priority = [
        'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull',
        'CreateContainerConfigError', 'CreateContainerError',
    ]
    reasons = [state.get('reason') for state in waiting if state.get('reason')]
    for reason in priority:
        if reason in reasons:
            return reason, 'danger', False
    if reasons:
        return reasons[0], 'warning', False
    terminated = [
        (container.get('state') or {}).get('terminated') or {}
        for container in status.get('containerStatuses') or []
    ]
    for state in terminated:
        if state.get('exitCode', 0) != 0:
            return state.get('reason', 'Error'), 'danger', False
    if phase == 'Running':
        return ('Running', 'good', True) if ready else ('NotReady', 'warning', False)
    return phase, 'warning', False


def base_pod_record(item, metrics):
    meta, status, spec = item.get('metadata') or {}, item.get('status') or {}, item.get('spec') or {}
    containers = spec.get('containers') or []
    stats = status.get('containerStatuses') or []
    state, severity, ready = pod_state(item)
    labels = meta.get('labels') or {}
    owners = meta.get('ownerReferences') or []
    owner = next((owner for owner in owners if owner.get('controller')), owners[0] if owners else {})
    micro = labels.get('app.kubernetes.io/name') or labels.get('app') or owner.get('name') or meta.get('name', '')
    if not labels.get('app.kubernetes.io/name') and not labels.get('app') and owner.get('kind') == 'ReplicaSet':
        micro = micro.rsplit('-', 1)[0]
    cpu, memory = metrics.get('cpu'), metrics.get('memory')
    limits = [container.get('resources', {}).get('limits', {}) for container in containers]
    cpu_limit = sum(number(value['cpu'], True) or 0 for value in limits) if limits and all('cpu' in value for value in limits) else None
    memory_limit = sum(number(value['memory']) or 0 for value in limits) if limits and all('memory' in value for value in limits) else None
    details = []
    by_name = {
        container.get('name'): container
        for container in stats + (status.get('initContainerStatuses') or [])
    }
    for container in containers + (spec.get('initContainers') or []):
        current = by_name.get(container['name'], {})
        details.append({
            'name': container['name'], 'image': container.get('image', ''),
            'ready': bool(current.get('ready')), 'restarts': current.get('restartCount', 0),
            'last_reason': current.get('lastState', {}).get('terminated', {}).get('reason', ''),
            'resources': container.get('resources') or {},
        })
    return {
        'name': meta.get('name', ''), 'namespace': meta.get('namespace', ''),
        'uid': meta.get('uid', ''), 'micro': micro, 'state': state,
        'severity': severity, 'is_ready': ready,
        'ready': f"{sum(bool(container.get('ready')) for container in stats)}/{len(containers)}",
        'restarts': sum(
            container.get('restartCount', 0)
            for container in stats + (status.get('initContainerStatuses') or [])
        ),
        'cpu': cpu, 'memory': memory, 'cpu_limit': cpu_limit, 'memory_limit': memory_limit,
        'node': spec.get('nodeName') or 'Sin asignar', 'ip': status.get('podIP') or '—',
        'age': age(meta.get('creationTimestamp')), 'created': meta.get('creationTimestamp', ''),
        'conditions': status.get('conditions') or [], 'containers': details,
    }


def pod_record(item, metrics):
    record = base_pod_record(item, metrics)
    spec = item.get('spec') or {}
    resident = (spec.get('containers') or []) + [
        container for container in spec.get('initContainers') or []
        if container.get('restartPolicy') == 'Always'
    ]
    for resource, key, is_cpu in [('cpu', 'cpu', True), ('memory', 'memory', False)]:
        for group, suffix in [('requests', 'request'), ('limits', 'limit')]:
            pod_value = (spec.get('resources') or {}).get(group, {}).get(resource)
            values = [
                number(container.get('resources', {}).get(group, {}).get(resource), is_cpu)
                for container in resident
            ]
            record[key + '_' + suffix] = (
                number(pod_value, is_cpu) if pod_value is not None
                else sum(values) if values and all(value is not None for value in values)
                else None
            )
    init_names = {container['name'] for container in spec.get('initContainers') or []}
    for container in record['containers']:
        container['kind'] = 'Init/sidecar' if container['name'] in init_names else 'Aplicación'
    record['overhead'] = spec.get('overhead') or {}
    record['resource_basis'] = 'Pod-level' if spec.get('resources') else 'Contenedores residentes'
    return record


def base_demo_snapshot(namespace, context):
    pods = []
    for index in range(9):
        micro = ['generador', 'renderizador', 'orquestador'][index // 3]
        state, severity, ready = 'Running', 'good', True
        if index == 3:
            state, severity, ready = 'CrashLoopBackOff', 'danger', False
        if index == 8:
            state, severity, ready = 'Pending', 'warning', False
        pods.append({
            'name': f'{micro}-7dc84c9f6-{["z7k2p", "m4r8w", "b9v3n"][index % 3]}',
            'namespace': 'generaciondocumentaldigital-qa', 'uid': f'demo-{index}',
            'micro': micro, 'state': state, 'severity': severity, 'is_ready': ready,
            'ready': '1/1' if ready else '0/1', 'restarts': 6 if index == 3 else (1 if index == 1 else 0),
            'cpu': None if index == 8 else [340, 275, 410, 25, 580, 420, 125, 98][index],
            'memory': None if index == 8 else [680, 590, 710, 130, 850, 780, 245, 225][index],
            'cpu_limit': 1000, 'memory_limit': 2048,
            'node': f'ip-10-0-{index % 3 + 1}-24.ec2.internal' if index != 8 else 'Sin asignar',
            'ip': f'10.4.1.{10 + index}' if index != 8 else '—',
            'age': '2d' if index != 8 else '1m', 'created': '2026-09-23T14:00:00Z',
            'conditions': [], 'containers': [{
                'name': micro, 'image': f'demo/{micro}:1.4.2', 'ready': ready,
                'restarts': 6 if index == 3 else 0,
                'last_reason': 'OOMKilled' if index == 3 else '',
                'resources': {
                    'requests': {'cpu': '250m', 'memory': '512Mi'},
                    'limits': {'cpu': '1', 'memory': '2Gi'},
                },
            }],
        })
    if namespace != 'generaciondocumentaldigital-qa':
        pods = []
    pods.sort(key=lambda pod: ({'danger': 0, 'warning': 1, 'good': 2}[pod['severity']], pod['name']))
    return {
        'pods': pods, 'context': context or 'demo-eks-qa', 'namespace': namespace,
        'metrics_error': None, 'updated': datetime.now().astimezone().isoformat(timespec='seconds'),
        'demo': True,
    }


def demo_snapshot(namespace, context):
    data = base_demo_snapshot(namespace, context)
    wave = 1 + .12 * math.sin(time.time() / 25)
    for pod in data['pods']:
        pod['cpu_request'] = 250 if pod['micro'] == 'orquestador' else 500
        pod['memory_request'] = 512 if pod['micro'] == 'orquestador' else 1024
        pod['resource_basis'], pod['overhead'] = 'Contenedores residentes', {}
        if pod['cpu'] is not None:
            pod['cpu'] = round(pod['cpu'] * wave, 2)
        if pod['memory'] is not None:
            pod['memory'] = round(pod['memory'] * (1 + .025 * math.sin(time.time() / 40)), 2)
    return data
