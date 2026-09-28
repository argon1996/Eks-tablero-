# Bancolombia EKS Console

Tablero local en Python para consultar Kubernetes/EKS con una interfaz gráfica estilo consola operativa.

## Funcionalidades

- Vista de pods, estados, readiness, reinicios, CPU, memoria y logs.
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

Guarda el proyecto en una carpeta definitiva y ejecuta en PowerShell, desde esa carpeta:

```powershell
.\install-shortcut.ps1
```

El instalador crea un acceso en el Escritorio y en el menú Inicio. Si tu Windows conserva la carpeta `Links`, también lo agrega a Favoritos del Explorador. Desde Inicio puedes anclarlo manualmente a la barra de tareas. Para abrir con datos simulados, ejecuta `.\launch-console.ps1 -Demo`; para usar tu script de conexión, `.\launch-console.ps1 -ConnectScript "C:\ruta\conectar-eks.ps1"`. El acceso directo abre la sesión actual de `kubectl` sin ejecutar automáticamente scripts ni pedir credenciales.

### Organización del código

- `pods_local.py`: entrada de línea de comandos y arranque local.
- `eks_console/backend.py`: consultas a EKS, inventario, métricas y captura de rendimiento.
- `eks_console/azure.py`: consultas a Azure DevOps y protección de variables secretas.
- `eks_console/server.py`: API HTTP local y validaciones de solicitudes.
- `eks_console/web/`: interfaz HTML, CSS y JavaScript.
- `launch-console.ps1` e `install-shortcut.ps1`: apertura e instalación del acceso de Windows.

La aplicación escucha únicamente en `127.0.0.1`. Para conectar AWS puede usar el contexto existente, ejecutar un script de conexión o pegar las variables temporales en la interfaz. Azure DevOps permite Microsoft Entra mediante `az login` o un PAT autorizado por la organización.

## Seguridad

- No se envían credenciales a un servidor externo.
- No se guardan tokens en archivos.
- Las variables secretas de Azure se muestran protegidas.
- La herramienta realiza consultas de lectura; no escala, reinicia ni modifica workloads.

## Validación

```powershell
python -m compileall -q pods_local.py eks_console
```
