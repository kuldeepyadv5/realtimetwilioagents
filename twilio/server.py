import asyncio
import os
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from twilio.rest import Client
import json

# --- CONFIG: use env vars; do NOT hardcode secrets ---
# Required: OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_CALLER_ID, PUBLIC_HOST

if TYPE_CHECKING:
    from twilio_handler import TwilioHandler
else:
    try:
        from .twilio_handler import TwilioHandler
    except ImportError:
        from twilio_handler import TwilioHandler

class TwilioWebSocketManager:
    def __init__(self):
        self.active_handlers = {}      # stream_sid -> handler
        self.pending_handlers = {}     # call_sid -> handler
        self.call_sessions = {}        # call_sid -> info
        self.calling_websockets = {}   # call_sid -> WebSocket

    async def new_session(self, websocket: WebSocket, call_sid: str | None = None) -> "TwilioHandler":
        handler = TwilioHandler(websocket)
        if call_sid and call_sid in self.call_sessions:
            self.pending_handlers[call_sid] = handler
            print(f"Created handler for outbound call: {call_sid}")
        else:
            print("Created handler for inbound call")
        return handler

    def get_handler_for_call(self, call_sid: str):
        return self.pending_handlers.get(call_sid)

    def register_stream_handler(self, stream_sid: str, handler: "TwilioHandler"):
        self.active_handlers[stream_sid] = handler

    def register_calling_websocket(self, call_sid: str, websocket: WebSocket):
        self.calling_websockets[call_sid] = websocket

    def cleanup_call(self, call_sid: str, stream_sid: str | None = None):
        self.pending_handlers.pop(call_sid, None)
        if stream_sid:
            self.active_handlers.pop(stream_sid, None)
        self.calling_websockets.pop(call_sid, None)
        self.call_sessions.pop(call_sid, None)

    async def notify_media_stream_connected(self, call_sid: str):
        ws = self.calling_websockets.get(call_sid)
        if not ws:
            return
        try:
            await ws.send_text(json.dumps({
                'event': 'media_stream_connected',
                'call_sid': call_sid,
                'message': 'Media stream connected - ready for conversation'
            }))
        except Exception as e:
            print(f"notify error: {e}")

    def get_twilio_client(self):
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        if not sid or not token:
            raise ValueError("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN")
        return Client(sid, token)

    async def make_outbound_call(self, phone_number: str) -> dict:
        try:
            client = self.get_twilio_client()
            caller_id = os.getenv("TWILIO_CALLER_ID")
            public_host = os.getenv("PUBLIC_HOST")  # e.g., https://<ngrok>.ngrok-free.app
            if not caller_id or not public_host:
                raise ValueError("Missing TWILIO_CALLER_ID or PUBLIC_HOST")

            to = phone_number.strip()
            if not to.startswith('+'):
                to = '+' + to

            # IMPORTANT: <Connect><Stream> creates a BIDIRECTIONAL stream.
            # No <Say> before/after — all audio is injected over WS.
            wss_host = public_host.replace('https://', '')
            twiml_response = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Response>
  <Connect>
    <Stream url=\"wss://{wss_host}/media-stream\" />
  </Connect>
</Response>"""

            call = client.calls.create(
                to=to,
                from_=caller_id,
                twiml=twiml_response,
                status_callback=f"{public_host}/call-status",
                status_callback_event=['initiated', 'ringing', 'answered', 'completed']
            )

            if not call or not call.sid:
                return {'success': False, 'error': 'Failed to create call'}

            self.call_sessions[call.sid] = {
                'call_sid': call.sid,
                'phone_number': to,
                'status': 'initiated',
                'timestamp': call.date_created.isoformat() if call.date_created else datetime.now().isoformat()
            }
            print(f"Call created: {call.sid}")
            return {'success': True, 'call_sid': call.sid, 'message': f'Call initiated to {to}'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

manager = TwilioWebSocketManager()
app = FastAPI()
app.mount("/static", StaticFiles(directory=".", html=True), name="static")

@app.get("/")
async def root():
    return {"message": "Twilio Media Stream Server is running!"}

@app.post("/incoming-call")
@app.get("/incoming-call")
async def incoming_call(request: Request):
    host = request.headers.get("Host")
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{host}/media-stream" />
  </Connect>
</Response>"""
    return PlainTextResponse(content=twiml_response, media_type="text/xml")

@app.post("/call-status")
async def call_status(request: Request):
    data = await request.form()
    call_sid = data.get('CallSid')
    status = data.get('CallStatus')
    print(f"Call {call_sid} status: {status}")
    if call_sid in manager.call_sessions:
        manager.call_sessions[call_sid]['status'] = status
        if status in ['completed', 'failed', 'busy', 'no-answer']:
            print(f"Call {call_sid} ended")
    return {"status": "received"}

@app.post("/make-call")
async def make_call_endpoint(request: Request):
    body = await request.json()
    phone_number = body.get('phone_number')
    if not phone_number:
        return {"success": False, "error": "Phone number is required"}
    return await manager.make_outbound_call(phone_number)


@app.websocket("/ws/calling-by-no")
async def calling_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    current_call_sid = None
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            if message.get('event') == 'start':
                result = await manager.make_outbound_call(message.get('phoneNumber'))
                if result.get('success'):
                    current_call_sid = result['call_sid']
                    manager.register_calling_websocket(current_call_sid, websocket)
                    await websocket.send_text(json.dumps({
                        'event': 'call_placed', 'call_sid': current_call_sid, 'message': result['message']
                    }))
                else:
                    await websocket.send_text(json.dumps({'event': 'call_error', 'message': result.get('error')}))
            elif message.get('event') == 'stop':
                await websocket.send_text(json.dumps({'event': 'call_ended', 'message': 'Call ended by user'}))
    except WebSocketDisconnect:
        pass
    finally:
        if current_call_sid:
            manager.cleanup_call(current_call_sid)


@app.websocket("/media-stream")
async def media_stream_endpoint(websocket: WebSocket):
    call_sid = None
    stream_sid = None
    handler = None
    try:
        await websocket.accept()
        connected = False
        while True:
            message = json.loads(await websocket.receive_text())
            event = message.get("event")
            if event == "connected":
                connected = True
            elif event == "start":
                start = message.get("start", {})
                stream_sid = start.get("streamSid")
                call_sid = start.get("callSid")
                handler = manager.get_handler_for_call(call_sid) or await manager.new_session(websocket, call_sid)
                # Pass streamSid to handler BEFORE it emits audio
                if hasattr(handler, 'set_stream_sid'):
                    handler.set_stream_sid(stream_sid)
                if stream_sid:
                    manager.register_stream_handler(stream_sid, handler)
                await handler.start()  # begins realtime + pumps audio (now with streamSid set)
                if call_sid:
                    await manager.notify_media_stream_connected(call_sid)
                await handler.wait_until_done()
                return
            # Ignore other events until start
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WS error: {e}")
    finally:
        if call_sid:
            manager.cleanup_call(call_sid, stream_sid)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)