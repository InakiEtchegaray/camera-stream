import cv2
import asyncio
import json
import logging
import mediapipe as mp
import numpy as np
from aiohttp import web
from av import VideoFrame
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from fractions import Fraction

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set para mantener las conexiones activas
pcs = set()

class VideoTransformTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self):
        super().__init__()
        logger.info("Iniciando cámara y detectores")
        
        # Inicializar cámara
        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        
        if not self.cap.isOpened():
            raise RuntimeError("No se pudo abrir la cámara")
        
        # Inicializar detectores de MediaPipe
        self.mp_drawing = mp.solutions.drawing_utils
        self.mp_pose = mp.solutions.pose
        self.mp_hands = mp.solutions.hands
        
        # Configurar detectores
        self.pose = self.mp_pose.Pose(
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5)
            
        self.hands = self.mp_hands.Hands(
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5)
        
        # Configurar tiempo base
        self.time_base = Fraction(1, 30)
        self.pts = 0
        
    def process_frame(self, frame):
        # Convertir a RGB para MediaPipe
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Detectar pose
        pose_results = self.pose.process(rgb_frame)
        if pose_results.pose_landmarks:
            self.mp_drawing.draw_landmarks(
                frame, 
                pose_results.pose_landmarks,
                self.mp_pose.POSE_CONNECTIONS,
                landmark_drawing_spec=self.mp_drawing.DrawingSpec(color=(0,255,0), thickness=2),
                connection_drawing_spec=self.mp_drawing.DrawingSpec(color=(255,0,0), thickness=2)
            )
            
        # Detectar manos
        hands_results = self.hands.process(rgb_frame)
        if hands_results.multi_hand_landmarks:
            for hand_landmarks in hands_results.multi_hand_landmarks:
                self.mp_drawing.draw_landmarks(
                    frame,
                    hand_landmarks,
                    self.mp_hands.HAND_CONNECTIONS,
                    landmark_drawing_spec=self.mp_drawing.DrawingSpec(color=(0,0,255), thickness=2),
                    connection_drawing_spec=self.mp_drawing.DrawingSpec(color=(255,255,0), thickness=2)
                )
                
        return frame
        
    async def recv(self):
        ret, frame = self.cap.read()
        
        if not ret:
            logger.error("Error al leer frame")
            return None
            
        # Procesar frame con detectores
        processed_frame = self.process_frame(frame)
            
        # Convertir frame para WebRTC
        frame = cv2.cvtColor(processed_frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame, format="rgb24")
        video_frame.pts = self.pts
        video_frame.time_base = self.time_base
        
        self.pts += 1
        return video_frame

    def __del__(self):
        if hasattr(self, 'cap') and self.cap is not None:
            self.cap.release()
        if hasattr(self, 'pose'):
            self.pose.close()
        if hasattr(self, 'hands'):
            self.hands.close()

# [El resto del código de offer() y index() permanece igual]

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

# Crear la aplicación
app = web.Application()
async def index(request):
    content = """
    <html>
    <head>
        <title>Stream de Cámara con Pose</title>
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
            <h1>Stream de Cámara con Pose</h1>
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
app.router.add_get("/", index)
app.router.add_post("/offer", offer)

if __name__ == "__main__":
    try:
        print("\n=== Servidor de streaming con detección de poses iniciado ===")
        print("Accede a http://localhost:8080 en tu navegador")
        print("Presiona Ctrl+C para detener el servidor\n")
        web.run_app(app, host="0.0.0.0", port=8080, access_log=None)
    except KeyboardInterrupt:
        print("\nServidor detenido por el usuario")
    finally:
        for pc in pcs:
            pc.close()
