"""Cliente Azure DevOps y filtrado de variables sensibles."""
import base64
import json
import re
import ssl
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import quote, urlencode, urlsplit

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
    def __init__(self, runner):
        self.runner=runner
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
                    self.token=self.runner(['az','account','get-access-token','--resource','499b84ac-1321-427f-aa17-267ca6975798','--query','accessToken','-o','tsv'],timeout=20).strip()
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
