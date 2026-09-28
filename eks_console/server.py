"""HTTP local: rutas, autorización y recursos estáticos."""
import json
import secrets
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from . import backend

LOCAL_TOKEN = secrets.token_urlsafe(32)
WEB = Path(__file__).resolve().parent / 'web'
STATIC = {
    '/static/app.css': ('app.css', 'text/css; charset=utf-8'),
    '/static/app.js': ('app.js', 'text/javascript; charset=utf-8'),
}

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
        if parsed.path == '/':
            page = (WEB / 'index.html').read_text(encoding='utf-8').replace('__LOCAL_TOKEN__', LOCAL_TOKEN)
            return self.respond(200, page.encode('utf-8'), 'text/html; charset=utf-8')
        if parsed.path in STATIC:
            filename, kind = STATIC[parsed.path]
            return self.respond(200, (WEB / filename).read_bytes(), kind)
        try:
            if parsed.path == '/api/health': result = {'app':'bancolombia-eks-console','version':'3.5','demo':backend.DEMO}
            elif parsed.path == '/api/config': result = backend.config_info(param('refresh') == 'true', param('details') == 'true')
            elif parsed.path == '/api/aws/status': result = backend.aws_connection_status(param('context'))
            elif parsed.path == '/api/performance': result=backend.PERF.status()
            elif parsed.path == '/api/azure/definitions': result=backend.azure_definitions()
            elif parsed.path == '/api/azure/definition': result=backend.azure_definition(param('kind'),param('id'))
            elif parsed.path == '/api/inventory': result=backend.inventory(param('namespace'),param('context'))
            elif parsed.path == '/api/pods': result = backend.list_pods(param('namespace', 'default'), param('context'), param('metrics', 'true') == 'true')
            elif parsed.path == '/api/pod-metrics': result = backend.pod_metrics(param('namespace', 'default'), param('context'))
            elif parsed.path in ('/api/logs', '/api/events'):
                result = backend.pod_content(parsed.path.rsplit('/', 1)[1], param('namespace'), param('pod'),
                    param('context'), param('container'), param('previous') == 'true', param('since'))
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
            if path=='/api/performance/config': result=backend.PERF.configure(data)
            elif path=='/api/performance/finish':result=backend.PERF.finish()
            elif path=='/api/performance/jtl':result=backend.PERF.import_results(data)
            elif path=='/api/aws/session':result=backend.aws_block_connect(data)
            elif path=='/api/aws/disconnect':
                context=str(data.get('context','')).strip()
                with backend.AWS_SESSION_LOCK:backend.AWS_SESSIONS.pop(context,None)
                result={'removed':True,'status':backend.aws_connection_status(context)}
            elif path=='/api/azure/connect': result=backend.azure_definitions() if backend.DEMO else backend.azure_block_connect(data)
            elif path=='/api/azure/disconnect':
                backend.AZURE.disconnect()
                if backend.PERF.mode=='auto':backend.PERF.finish()
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
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try: self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError): pass

    def log_message(self, *args): pass
