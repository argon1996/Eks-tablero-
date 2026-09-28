# Bancolombia EKS Console

Tablero local en Python para consultar Kubernetes/EKS con una interfaz gráfica estilo consola operativa.

## Funcionalidades

- Vista de pods, estados, readiness, reinicios, CPU y memoria.
- Carga progresiva: la tabla de pods aparece sin esperar a Metrics Server y muestra el tiempo real de cada consulta.
- Conexión directa mediante la sesión local o un bloque temporal, con una sola validación y mensajes breves.
- Actualización cada 30 segundos que conserva la última lectura ante una demora temporal de VPN o EKS.
- Logs por contenedor con consulta puntual, captura continua, limpieza y copia al portapapeles; no genera archivos.
- Inventario de recursos por namespace con manejo de permisos parciales.
- Vista **Performance** con CPU/memoria frente a requests y limits, HPA, réplicas y captura periódica.
- Importación de resultados JMeter (`.jtl`/`.csv`) para calcular P50, P95, P99, errores y throughput.
- Integración opcional de Azure DevOps para consultar pipelines, ambientes y variables accesibles.
- Variables secretas protegidas y credenciales AWS/Azure mantenidas únicamente en memoria.
- Modo demo para revisar la interfaz sin conectarse al banco.

## Requisitos

- Python 3.9 o superior.
- `kubectl` configurado para el contexto EKS.
- Metrics Server para CPU y memoria (`kubectl top pods`).
- Opcional: AWS CLI, Azure CLI (`az`) y acceso a Azure DevOps.

## Uso

```powershell
py pods_local.py --demo
py pods_local.py
py pods_local.py --connect-script "C:\ruta\conectar-eks.ps1"
```

### Acceso directo en Windows

Ejecuta en PowerShell, desde la carpeta descargada:

```powershell
.\install-shortcut.ps1
```

El instalador copia la aplicación a `%LOCALAPPDATA%\Programs\Bancolombia EKS Console`, valida Python y crea accesos en el Escritorio, el menú Inicio y la ubicación de aplicaciones ancladas de la barra de tareas. El acceso abre Python directamente, sin omitir políticas de PowerShell. Algunas políticas de Windows exigen anclarla manualmente desde Inicio la primera vez. Para abrir con datos simulados, ejecuta `.\launch-console.ps1 -Demo`; para usar tu script de conexión, `.\launch-console.ps1 -ConnectScript "C:\ruta\conectar-eks.ps1"`.

La conexión recomendada usa la sesión actual configurada por AWS CLI y `kubeconfig`. Como alternativa, la interfaz admite un bloque temporal con `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` y `AWS_SESSION_TOKEN`: la identidad se valida con AWS antes de marcarla como conectada y los valores permanecen únicamente en memoria.

### Organización del código

- `pods_local.py`: entrada de línea de comandos y arranque local.
- `eks_console/backend.py`: fachada de consultas EKS e inventario usada por la API.
- `eks_console/kubernetes_models.py`: estados, recursos y modelos de pods.
- `eks_console/aws_auth.py`: sesiones AWS y credenciales temporales en memoria.
- `eks_console/performance.py`: captura de rendimiento, JMeter y análisis.
- `eks_console/azure.py`: consultas a Azure DevOps y protección de variables secretas.
- `eks_console/server.py`: API HTTP local y validaciones de solicitudes.
- `eks_console/web/`: interfaz HTML, CSS y JavaScript.
- `launch-console.ps1` e `install-shortcut.ps1`: apertura e instalación del acceso de Windows.

La aplicación escucha únicamente en `127.0.0.1`. Para conectar AWS puede usar el contexto existente, ejecutar un script de conexión o pegar las variables temporales en la interfaz. Azure DevOps permite Microsoft Entra mediante `az login` o un PAT autorizado por la organización.

Solo se mantiene una instancia por puerto. Si vuelves a abrir EKS Console mientras ya está activa, el lanzador reconoce su endpoint local y abre la sesión existente en lugar de crear otro proceso. Un servicio distinto que ocupe el mismo puerto nunca se reutiliza como si fuera la consola.

## Seguridad

- No se envían credenciales a un servidor externo.
- No se guardan tokens en archivos.
- Las consultas globales (`-A`) están bloqueadas: la operación exige un namespace exacto y nunca usa `ListClusters` ni enumera namespaces.
- La vista principal hace únicamente dos lecturas paralelas en el namespace seleccionado: pods y métricas. No repite lecturas mientras otra actualización sigue activa.
- La interfaz no inicia descargas: los logs y análisis se copian al portapapeles solo cuando el usuario lo solicita.
- El acceso directo abre Python directamente; no ejecuta scripts PowerShell ni altera su política de ejecución.
- Las variables secretas de Azure se muestran protegidas.
- La herramienta realiza consultas de lectura; no escala, reinicia ni modifica workloads.

## Validación

```powershell
python -m compileall -q pods_local.py eks_console
python -m unittest discover -s tests -v
```
