import cv2
import asyncio
import json
import logging
from aiohttp import web
from av import VideoFrame
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from fractions import Fraction

# Configuración de logging
logging.basicConfig(level=logging.INFO)  # Cambiado a INFO para reducir logs
logger = logging.getLogger(__name__)

# Set para mantener las conexiones activas
pcs = set()

class VideoTransformTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self):
        super().__init__()
        logger.info("Iniciando cámara")
        
        self.cap = cv2.VideoCapture(0)
        
        # Configurar la cámara
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        
        if not self.cap.isOpened():
            raise RuntimeError("No se pudo abrir la cámara")
            
        # Configurar el tiempo base para los frames
        self.time_base = Fraction(1, 30)  # 30 FPS
        self.pts = 0
        
    async def recv(self):
        ret, frame = self.cap.read()
        
        if not ret:
            logger.error("Error al leer frame")
            return None
            
        # Convertir frame para WebRTC
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = self.pts
        video_frame.time_base = self.time_base
        
        self.pts += 1
        return video_frame

    def __del__(self):
        if hasattr(self, 'cap') and self.cap is not None:
            self.cap.release()

async def offer(request):
    params = await request.json()
    
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    
    pc = RTCPeerConnection()
    pcs.add(pc)
    
    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info(f"Estado de conexión WebRTC: {pc.connectionState}")
        if pc.connectionState == "failed":
            await pc.close()
            pcs.discard(pc)
    
    try:
        video = VideoTransformTrack()
        pc.addTrack(video)
        
        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        
        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type
            })
        )
    except Exception as e:
        logger.error(f"Error en el proceso de offer: {str(e)}")
        return web.Response(status=500, text=str(e))

async def index(request):
    content = """
    <html>
    <head>
        <title>Stream de Cámara</title>
        <style>
            body { 
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 20px;
                background-color: #f0f0f0;
            }
            .container {
                max-width: 800px;
                margin: 0 auto;
                text-align: center;
            }
            #videoContainer {
                position: relative;
                width: 100%;
                max-width: 640px;
                margin: 20px auto;
            }
            #video {
                width: 100%;
                border: 3px solid #333;
                border-radius: 8px;
                background-color: #000;
            }
            #stats {
                position: absolute;
                top: 10px;
                left: 10px;
                background: rgba(0,0,0,0.7);
                color: white;
                padding: 5px;
                border-radius: 4px;
                font-size: 12px;
            }
            #status {
                margin: 10px;
                padding: 10px;
                border-radius: 4px;
                background-color: #fff;
                display: inline-block;
            }
            .error { color: red; font-weight: bold; }
            .success { color: green; font-weight: bold; }
            .connecting { color: orange; font-weight: bold; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Stream de Cámara</h1>
            <div id="videoContainer">
                <video id="video" autoplay playsinline></video>
                <div id="stats"></div>
            </div>
            <div id="status" class="connecting">Iniciando conexión...</div>
        </div>
        
        <script>
        let pc = null;
        const status = document.getElementById('status');
        const video = document.getElementById('video');
        const stats = document.getElementById('stats');
        
        async function startStream() {
            if (pc) {
                pc.close();
            }
            
            status.textContent = 'Conectando...';
            status.className = 'connecting';
            
            try {
                pc = new RTCPeerConnection({
                    iceServers: [
                        { urls: 'stun:stun.l.google.com:19302' }
                    ]
                });
                
                pc.oniceconnectionstatechange = () => {
                    status.textContent = 'Estado: ' + pc.iceConnectionState;
                    if (pc.iceConnectionState === 'connected') {
                        status.className = 'success';
                    }
                };
                
                pc.ontrack = function(event) {
                    status.textContent = 'Conectado';
                    status.className = 'success';
                    video.srcObject = event.streams[0];
                };
                
                const offer = await pc.createOffer({
                    offerToReceiveVideo: true,
                    offerToReceiveAudio: false
                });
                
                await pc.setLocalDescription(offer);
                
                const response = await fetch('/offer', {
                    body: JSON.stringify({
                        sdp: pc.localDescription.sdp,
                        type: pc.localDescription.type
                    }),
                    headers: {'Content-Type': 'application/json'},
                    method: 'POST'
                });
                
                const answer = await response.json();
                await pc.setRemoteDescription(answer);
                
            } catch (e) {
                status.textContent = 'Error: ' + e.toString();
                status.className = 'error';
                console.error(e);
            }
        }
        
        // Iniciar stream cuando carga la página
        startStream();
        
        // Actualizar estadísticas cada segundo
        setInterval(async () => {
            if (pc && video.srcObject) {
                const stats = await pc.getStats();
                stats.forEach(report => {
                    if (report.type === 'inbound-rtp' && report.kind === 'video') {
                        const statsText = `FPS: ${report.framesPerSecond || 0}`;
                        document.getElementById('stats').textContent = statsText;
                    }
                });
            }
        }, 1000);
        </script>
    </body>
    </html>
    """
    return web.Response(content_type="text/html", text=content)

# Crear la aplicación
app = web.Application()
app.router.add_get("/", index)
app.router.add_post("/offer", offer)

if __name__ == "__main__":
    try:
        print("\n=== Servidor de streaming iniciado ===")
        print("Accede a http://localhost:8080 en tu navegador")
        print("Presiona Ctrl+C para detener el servidor\n")
        web.run_app(app, host="0.0.0.0", port=8080, access_log=None)  # access_log=None para reducir logs
    except KeyboardInterrupt:
        print("\nServidor detenido por el usuario")
    finally:
        for pc in pcs:
            pc.close()