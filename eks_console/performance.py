"""Importación JMeter, análisis y captura periódica de rendimiento."""

import csv
import io
import math
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone


def percentile(values, proportion):
    return sorted(values)[max(0, math.ceil(len(values) * proportion) - 1)] if values else None


def parse_jtl(text, label_filter=''):
    if len(text) > 10_000_000:
        raise ValueError('El archivo supera 10 MB. Exporta la prueba o un intervalo más pequeño.')
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not {'timeStamp', 'elapsed', 'success'}.issubset(set(reader.fieldnames or [])):
        raise ValueError(
            'Importa JTL/CSV de muestras con encabezados timeStamp, elapsed y success; '
            'no un resumen agregado.'
        )
    rows, labels = [], set()
    for row in reader:
        label = row.get('label', '')
        labels.add(label)
        if label_filter and label != label_filter:
            continue
        if len(rows) >= 200_000:
            raise ValueError('Máximo 200.000 muestras por archivo.')
        try:
            timestamp, elapsed = float(row['timeStamp']), float(row['elapsed'])
            if row['success'].lower() not in ('true', 'false'):
                raise ValueError('success')
            success = row['success'].lower() == 'true'
            if not math.isfinite(timestamp) or not math.isfinite(elapsed) or timestamp <= 0 or elapsed < 0:
                raise ValueError()
            if float(row.get('SampleCount') or row.get('sampleCount') or 1) != 1:
                raise ValueError('aggregated')
        except (TypeError, ValueError):
            raise ValueError(
                'Hay muestras inválidas o agregadas; usa muestras individuales de JMeter.'
            ) from None
        rows.append((timestamp, elapsed, success, label))
    if not rows:
        raise ValueError('No hay muestras para el filtro indicado.')
    durations = [row[1] for row in rows]
    start = min(row[0] for row in rows)
    end = max(row[0] + row[1] for row in rows)
    seconds = (end - start) / 1000
    buckets = defaultdict(list)
    bucket_size = max(1000, math.ceil((end - start) / 100) * 1.0)
    for row in rows:
        buckets[int((row[0] - start) // bucket_size)].append(row)
    series = [{
        'time': (start + key * bucket_size) / 1000,
        'p95': percentile([row[1] for row in bucket], .95),
        'samples': len(bucket),
        'errors': sum(not row[2] for row in bucket),
    } for key, bucket in sorted(buckets.items())]
    errors = sum(not row[2] for row in rows)
    return {
        'samples': len(rows), 'errors': errors, 'error_percent': 100 * errors / len(rows),
        'p50': percentile(durations, .50), 'p95': percentile(durations, .95),
        'p99': percentile(durations, .99), 'max': max(durations),
        'average': sum(durations) / len(durations),
        'throughput': len(rows) / seconds if seconds else None,
        'duration_s': seconds, 'start': start / 1000, 'end': end / 1000,
        'labels': sorted(labels)[:200], 'label_filter': label_filter, 'series': series,
        'note': 'Cada fila cuenta como una muestra, no necesariamente un documento. '
                'Se asume timeStamp al inicio. Filtra una etiqueta si el archivo contiene '
                'muestras padre e hijas para evitar doble conteo.',
    }


def analyze(samples, latest, jtl=None, slo=None):
    findings = []
    slo = slo or {'p95_ms': 1000, 'errors_percent': 1}

    def add(level, title, evidence, action):
        findings.append({'level': level, 'title': title, 'evidence': evidence, 'action': action})

    if not samples:
        add('info', 'Faltan muestras', 'Aún no hay mediciones de esta captura.',
            'Inicia una captura manual o arma una ejecución de Azure DevOps.')
    else:
        duration = samples[-1]['time'] - samples[0]['time']
        complete = sum(
            sample.get('measured', 0) == sample.get('total', 0) and sample.get('total', 0) > 0
            for sample in samples
        )
        gaps = [
            second['time'] - first['time']
            for first, second in zip(samples, samples[1:])
            if second['time'] - first['time'] > 45
        ]
        if gaps:
            add('warning', 'Intervalos sin observación continua',
                f'{len(gaps)} pausas de más de 45 s entre muestras.',
                'Revisa errores de conexión; las curvas no reconstruyen esas pausas.')
        if len(samples) < 20 or duration < 300:
            add('info', 'Ventana de observación corta',
                f'{len(samples)} muestras en {duration / 60:.1f} min.',
                'Captura calentamiento, carga sostenida y recuperación antes de ajustar recursos.')
        if complete < len(samples):
            add('warning', 'Cobertura parcial de métricas',
                f'{complete}/{len(samples)} muestras tienen CPU y memoria para todos los pods.',
                'Revisa acceso a metrics.k8s.io; un dato ausente no representa consumo cero.')
        for key, label in [('cpu', 'CPU'), ('memory', 'Memoria')]:
            observed = [sample[key] for sample in samples if sample.get(key) is not None]
            ratios = [
                sample[key] / sample[key + '_limit'] * 100
                for sample in samples
                if sample.get(key) is not None and sample.get(key + '_limit', 0) > 0
            ]
            if observed:
                unit = 'mCPU' if key == 'cpu' else 'MiB'
                evidence = (
                    f'P95 de la suma: {percentile(observed, .95):.1f} {unit}; '
                    f'máximo: {max(observed):.1f} {unit}.'
                )
                if ratios:
                    evidence += f' Pico/límite declarado: {max(ratios):.0f}%.'
                add('warning' if ratios and max(ratios) >= 85 else 'info',
                    f'{label}: consumo observado', evidence,
                    'Revisa también cada pod; el agregado puede ocultar un contenedor saturado.')
        increases = sum(sample.get('restart_delta', 0) for sample in samples)
        if increases:
            add('danger', 'Reinicios durante la captura',
                f'Se observaron {increases} incrementos en los contadores.',
                'Correlaciona eventos, última terminación y logs anteriores.')
        readiness = [sample for sample in samples if sample.get('ready', 0) < sample.get('total', 0)]
        if readiness:
            add('warning', 'Pods sin Ready',
                f'{len(readiness)}/{len(samples)} muestras incluyeron pods no listos.',
                'Revisa tiempos de arranque, probes, eventos y disponibilidad.')
    if latest:
        for pod in latest.get('pods', []):
            for key, label in [('cpu', 'CPU'), ('memory', 'Memoria')]:
                if pod.get(key) is not None and pod.get(key + '_limit') and pod[key] / pod[key + '_limit'] >= .85:
                    add('warning', f'{label} cerca del límite en un pod',
                        pod['name'] + f': {pod[key] / pod[key + "_limit"] * 100:.0f}% del límite.',
                        'Consulta el contenedor y valida el dato contra la carga.')
        for hpa in latest.get('hpas', []):
            if hpa.get('max') and hpa.get('current') is not None and hpa['current'] >= hpa['max']:
                add('warning', 'HPA en máximo de réplicas',
                    hpa['name'] + f': {hpa["current"]}/{hpa["max"]} réplicas.',
                    'Comprueba latencia y capacidad antes de cambiar maxReplicas.')
    if jtl:
        passed = jtl['p95'] <= slo['p95_ms'] and jtl['error_percent'] <= slo['errors_percent']
        add('good' if passed else 'danger', 'Resultado frente a tus objetivos',
            f'P95 {jtl["p95"]:.0f} ms (objetivo ≤ {slo["p95_ms"]:g}); '
            f'errores {jtl["error_percent"]:.2f}% (objetivo ≤ {slo["errors_percent"]:g}%).',
            'Confirma que el archivo y la etiqueta pertenecen a esta ejecución.')
        if samples and (jtl['end'] < samples[0]['time'] or jtl['start'] > samples[-1]['time']):
            add('warning', 'JTL fuera del intervalo observado',
                'El archivo no coincide temporalmente con las métricas capturadas.',
                'Selecciona la ejecución correcta antes de atribuir consumo y latencia.')
    else:
        add('info', 'Latencia y tasa de errores pendientes',
            'Las métricas de Kubernetes no contienen resultados de peticiones.',
            'Importa el JTL/CSV para calcular percentiles, throughput y errores.')
    return findings[:30]


class PerformanceMonitor:
    """Captura métricas periódicas con dependencias inyectadas por el backend."""

    def __init__(self, scope_validator, snapshot_reader, azure_client, demo_provider):
        self.scope_validator = scope_validator
        self.snapshot_reader = snapshot_reader
        self.azure = azure_client
        self.is_demo = demo_provider
        self.lock = threading.RLock()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.generation = 0
        self.scope = None
        self.mode = 'idle'
        self.selection = None
        self.latest = None
        # 12 h at a 15 s cadence. This remains bounded and entirely in memory.
        self.samples = deque(maxlen=2880)
        self.seen_restarts = {}
        self.run = None
        self.run_latest = None
        self.run_samples = deque(maxlen=2880)
        self.last_completed_key = None
        self.error = self.azure_error = ''
        self.jtl = None
        self.slo = {'p95_ms': 1000, 'errors_percent': 1}
        self.started = self.ended = None
        self.last_azure_poll = 0
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def configure(self, data):
        namespace = str(data.get('namespace', '')).strip()
        self.scope_validator(namespace)
        context = str(data.get('context', '')).strip()
        if not context:
            raise ValueError('Selecciona el contexto EKS que corresponde al ambiente.')
        mode = data.get('mode', 'live')
        if mode not in ('live', 'manual', 'auto'):
            raise ValueError('Modo inválido.')
        selection = data.get('selection') or {}
        if mode == 'auto':
            if selection.get('kind') not in ('build', 'release') or not str(selection.get('definition_id', '')).isdigit():
                raise ValueError('Selecciona un pipeline antes de armar la captura automática.')
            if selection.get('kind') == 'release' and not str(selection.get('stage', '')).strip():
                raise ValueError('Selecciona el ambiente del Release para vincularlo con EKS.')
            if not self.is_demo() and not self.azure.summary()['connected']:
                raise ValueError('Conecta Azure DevOps antes de armar la captura.')
        with self.lock:
            self.generation += 1
            micro_prefix = str(data.get('micro_prefix', '')).strip()
            if len(micro_prefix) > 80:
                raise ValueError('El prefijo del microservicio es demasiado largo.')
            self.scope = {
                'namespace': namespace, 'context': context,
                'micro': str(data.get('micro', '')).strip(),
                'micro_prefix': micro_prefix,
            }
            self.mode, self.selection, self.latest = mode, selection, None
            self.samples.clear()
            self.seen_restarts = {}
            self.run_samples.clear()
            self.run = self.run_latest = None
            self.error = self.azure_error = ''
            self.jtl = self.last_completed_key = None
            self.started = time.time() if mode == 'manual' else None
            self.ended, self.last_azure_poll = None, 0
        self.wake.set()
        return self.status()

    def finish(self):
        with self.lock:
            self.mode, self.ended = 'live', time.time()
            if self.run:
                self.run['active'] = False
                self.run['status'] = 'captura detenida'
        return self.status()

    def import_results(self, data):
        jtl = parse_jtl(str(data.get('csv', '')), str(data.get('label', '')).strip())
        p95, errors = float(data.get('p95_ms', 1000)), float(data.get('errors_percent', 1))
        if not math.isfinite(p95) or p95 <= 0 or not math.isfinite(errors) or not 0 <= errors <= 100:
            raise ValueError('Objetivos inválidos.')
        with self.lock:
            self.jtl, self.slo = jtl, {'p95_ms': p95, 'errors_percent': errors}
        return self.status()

    def status(self, window_seconds=None):
        with self.lock:
            observed = list(self.run_samples if self.started else self.samples)
            if window_seconds is not None:
                try:
                    window_seconds = max(300, min(43_200, int(window_seconds)))
                except (TypeError, ValueError):
                    window_seconds = 1800
                # Anchor the window to the latest sample so a finished capture remains reviewable.
                cutoff = (observed[-1].get('time', time.time()) if observed else time.time()) - window_seconds
                observed = [sample for sample in observed if sample.get('time', 0) >= cutoff]
            return {
                'mode': self.mode, 'scope': self.scope, 'selection': self.selection,
                'latest': self.latest, 'samples': observed, 'run': self.run,
                'started': self.started, 'ended': self.ended, 'error': self.error,
                'azure_error': self.azure_error, 'jtl': self.jtl, 'slo': self.slo,
                'demo': self.is_demo(),
                'analysis': analyze(
                    observed, self.run_latest if self.started else self.latest,
                    self.jtl, self.slo,
                ),
                'window_seconds': window_seconds,
                'coverage_note': 'Muestras locales cada 15 s, hasta 2.880 (aprox. 12 h). '
                                 'No se reconstruye historia anterior.',
            }

    def loop(self):
        while not self.stop_event.is_set():
            self.wake.wait(15)
            self.wake.clear()
            if self.stop_event.is_set():
                break
            with self.lock:
                target = dict(self.scope) if self.scope else None
                generation, mode = self.generation, self.mode
                selection = dict(self.selection or {})
                tracked = dict(self.run) if self.run and self.run.get('active') else None
            if not target:
                continue
            now = time.time()
            if mode == 'auto' and now - self.last_azure_poll >= 30:
                try:
                    run = ({'id': 2048, 'key': 'demo:2048', 'name': 'Performance QA · ejemplo',
                            'active': True, 'status': 'inProgress',
                            'start': datetime.now(timezone.utc).isoformat()}
                           if self.is_demo() else self.azure.poll(selection, tracked))
                    with self.lock:
                        if generation != self.generation:
                            continue
                        self.azure_error, self.last_azure_poll = '', now
                        if run and run['active'] and run['key'] != self.last_completed_key:
                            if not self.run or self.run['key'] != run['key']:
                                self.run_samples.clear()
                                self.seen_restarts = {}
                                self.run_latest = self.jtl = None
                                self.started, self.ended = now, None
                            self.run = run
                        elif self.run and self.run.get('active'):
                            if run is None:
                                self.azure_error = 'Azure no devolvió la ejecución seguida.'
                            else:
                                self.run, self.ended, self.last_completed_key = run, now, run['key']
                except (RuntimeError, ValueError, OSError) as exc:
                    with self.lock:
                        self.azure_error, self.last_azure_poll = str(exc), now
            try:
                current = self.snapshot_reader(**target)
                with self.lock:
                    if generation != self.generation:
                        continue
                    self.latest = current
                    point = {key: value for key, value in current['sample'].items() if key != 'restarts_by_uid'}
                    counters = current['sample'].get('restarts_by_uid', {})
                    point['restart_delta'] = sum(
                        max(0, value - self.seen_restarts.get(key, value))
                        for key, value in counters.items()
                    )
                    self.seen_restarts.update(counters)
                    if len(self.seen_restarts) > 10_000:
                        self.seen_restarts = dict(counters)
                    self.samples.append(point)
                    self.error = ''
                    if self.mode == 'manual' or (self.mode == 'auto' and self.run and self.run.get('active')):
                        self.run_samples.append(point)
                        self.run_latest = current
            except (RuntimeError, ValueError, OSError) as exc:
                with self.lock:
                    if generation == self.generation:
                        self.error = str(exc)

    def close(self):
        self.stop_event.set()
        self.wake.set()
