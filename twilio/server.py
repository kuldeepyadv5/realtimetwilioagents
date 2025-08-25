import asyncio
import os
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
import json

import os
# from django.conf import settings

# Import TwilioHandler class - handle both module and package use cases
if TYPE_CHECKING:
    # For type checking, use the relative import
    from .twilio_handler import TwilioHandler
else:
    # At runtime, try both import styles
    try:
        # Try relative import first (when used as a package)
        from .twilio_handler import TwilioHandler
    except ImportError:
        # Fall back to direct import (when run as a script)
        from twilio_handler import TwilioHandler


class TwilioWebSocketManager:
    def __init__(self):
        self.active_handlers: dict[str, TwilioHandler] = {}  # stream_sid -> handler
        self.pending_handlers: dict[str, TwilioHandler] = {}  # call_sid -> handler (for outbound calls)
        self.call_sessions: dict[str, dict] = {}  # Track call states
        self.calling_websockets: dict[str, WebSocket] = {}  # call_sid -> calling interface websocket

    async def new_session(self, websocket: WebSocket, call_sid: str = None) -> TwilioHandler:
        """Create and configure a new session."""
        print("Creating twilio handler")

        handler = TwilioHandler(websocket)

        # If this is for an outbound call, store it in pending handlers
        if call_sid and call_sid in self.call_sessions:
            self.pending_handlers[call_sid] = handler
            print(f"Created handler for outbound call: {call_sid}")
        else:
            print("Created handler for inbound call")

        return handler

    def get_handler_for_call(self, call_sid: str) -> TwilioHandler | None:
        """Get the handler for a specific call."""
        return self.pending_handlers.get(call_sid)

    def register_stream_handler(self, stream_sid: str, handler: TwilioHandler):
        """Register a handler for a specific stream."""
        self.active_handlers[stream_sid] = handler
        print(f"Registered handler for stream: {stream_sid}")

    def register_calling_websocket(self, call_sid: str, websocket: WebSocket):
        """Register the calling interface WebSocket for a call."""
        self.calling_websockets[call_sid] = websocket
        print(f"Registered calling WebSocket for call: {call_sid}")

    def cleanup_call(self, call_sid: str, stream_sid: str = None):
        """Clean up handlers for a call."""
        if call_sid in self.pending_handlers:
            del self.pending_handlers[call_sid]
            print(f"Cleaned up pending handler for call: {call_sid}")

        if stream_sid and stream_sid in self.active_handlers:
            del self.active_handlers[stream_sid]
            print(f"Cleaned up active handler for stream: {stream_sid}")

        if call_sid in self.calling_websockets:
            del self.calling_websockets[call_sid]
            print(f"Cleaned up calling WebSocket for call: {call_sid}")

        if call_sid in self.call_sessions:
            del self.call_sessions[call_sid]
            print(f"Cleaned up call session: {call_sid}")

    async def notify_media_stream_connected(self, call_sid: str):
        """Notify the calling interface that media stream is connected."""
        if call_sid in self.calling_websockets:
            websocket = self.calling_websockets[call_sid]
            try:
                response = {
                    'event': 'media_stream_connected',
                    'call_sid': call_sid,
                    'message': 'Media stream connected - ready for conversation'
                }
                await websocket.send_text(json.dumps(response))
                print(f"Notified calling interface of media stream connection for call: {call_sid}")
            except Exception as e:
                print(f"Error notifying calling interface: {e}")

    def get_twilio_client(self):
        """Get Twilio client instance."""
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")

        if not account_sid or not auth_token:
            raise ValueError("Twilio credentials not found in environment variables")

        print(f"Creating Twilio client with SID: {account_sid[:10]}...")
        return Client(account_sid, auth_token)

    async def make_outbound_call(self, phone_number: str) -> dict:
        """Make an outbound call using Twilio."""
        try:
            client = self.get_twilio_client()
            caller_id = os.getenv("TWILIO_CALLER_ID")
            public_host = os.getenv("PUBLIC_HOST")

            if not caller_id or not public_host:
                raise ValueError("TWILIO_CALLER_ID or PUBLIC_HOST not configured")

            # Clean and validate phone number
            phone_number = phone_number.strip()
            if not phone_number.startswith('+'):
                phone_number = '+' + phone_number

            print(f"Making outbound call from {caller_id} to {phone_number}")

            # Create TwiML for outbound call - connect immediately to media stream
            twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
            <Response>
                <Say>hii kuldeep how are you</Say>
                <Connect>
                    <Stream url="wss://{public_host.replace('https://', '')}/media-stream" />
                </Connect>
            </Response>"""

            # Make the outbound call
            try:
                call = client.calls.create(
                    to=phone_number,
                    from_=caller_id,
                    twiml=twiml_response,
                    status_callback=f"{public_host}/call-status",
                    status_callback_event=['initiated', 'ringing', 'answered', 'completed']
                )
            except Exception as twilio_error:
                print(f"Twilio API error: {twilio_error}")
                return {
                    'success': False,
                    'error': f'Twilio API error: {str(twilio_error)}'
                }

            # Validate call object
            if not call or not call.sid:
                print(f"Invalid call object: {call}")
                return {
                    'success': False,
                    'error': 'Failed to create call - no call SID returned'
                }

            print(f"Call object attributes: sid={call.sid}, date_created={call.date_created}")

            # Store call information
            call_info = {
                'call_sid': call.sid,
                'phone_number': phone_number,
                'status': 'initiated',
                'timestamp': call.date_created.isoformat() if call.date_created else datetime.now().isoformat()
            }

            self.call_sessions[call.sid] = call_info

            print(f"\nCall created successfully: {call.sid}")
            return {
                'success': True,
                'call_sid': call.sid,
                'message': f'Call initiated to {phone_number}'
            }

        except Exception as e:
            print(f"Error making outbound call: {e}")
            import traceback
            traceback.print_exc()
            return {
                'success': False,
                'error': str(e)
            }

    # In a real app, you'd also want to clean up/close the handler when the call ends


manager = TwilioWebSocketManager()
app = FastAPI()

# Mount static files directory
app.mount("/static", StaticFiles(directory=".", html=True), name="static")


@app.get("/")
async def root():
    return {"message": "Twilio Media Stream Server is running!"}


@app.post("/incoming-call")
@app.get("/incoming-call")
async def incoming_call(request: Request):
    """Handle incoming Twilio phone calls"""
    host = request.headers.get("Host")

    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Hello! You're now connected to an AI assistant. You can start talking!</Say>
    <Connect>
        <Stream url="wss://{host}/media-stream" />
    </Connect>
</Response>"""
    return PlainTextResponse(content=twiml_response, media_type="text/xml")


@app.post("/call-status")
async def call_status(request: Request):
    """Handle call status updates from Twilio."""
    form_data = await request.form()
    call_sid = form_data.get('CallSid')
    call_status = form_data.get('CallStatus')

    print(f"Call {call_sid} status: {call_status}")

    if call_sid in manager.call_sessions:
        manager.call_sessions[call_sid]['status'] = call_status

        # Notify the calling interface about status changes
        if call_status in ['completed', 'failed', 'busy', 'no-answer']:
            print(f"Call {call_sid} ended with status: {call_status}")
            # Cleanup will happen when WebSocket disconnects

    return {"status": "received"}


@app.post("/make-call")
async def make_call_endpoint(request: Request):
    """API endpoint to make outbound calls."""
    try:
        data = await request.json()
        phone_number = data.get('phone_number')
        print("make_call_endpoint_phone_number", phone_number)

        if not phone_number:
            return {"success": False, "error": "Phone number is required"}

        result = await manager.make_outbound_call(phone_number)
        return result

    except Exception as e:
        print(f"Error in make_call_endpoint: {e}")
        return {"success": False, "error": str(e)}


@app.websocket("/ws/calling-by-no")
async def calling_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for the calling interface."""
    await websocket.accept()
    print("\nCalling interface WebSocket connected")

    current_call_sid = None

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            print(f"\nReceived message: {message}")

            if message.get('event') == 'start':
                phone_number = message.get('phoneNumber')
                if phone_number:
                    print(f"Initiating call to: {phone_number}")
                    result = await manager.make_outbound_call(phone_number)
                    print(f"Call result: {result}")

                    if result.get('success'):
                        current_call_sid = result.get('call_sid')
                        # Register this WebSocket for the call
                        manager.register_calling_websocket(current_call_sid, websocket)

                        response = {
                            'event': 'call_placed',
                            'call_sid': current_call_sid,
                            'message': result.get('message', 'Call initiated')
                        }
                    else:
                        response = {
                            'event': 'call_error',
                            'message': result.get('error', 'Failed to place call')
                        }

                    await websocket.send_text(json.dumps(response))

            elif message.get('event') == 'stop':
                call_sid = message.get('call_sid')
                if call_sid:
                    # Handle call stop if needed
                    print(f"Stopping call: {call_sid}")

                response = {
                    'event': 'call_ended',
                    'message': 'Call ended by user'
                }
                await websocket.send_text(json.dumps(response))

    except WebSocketDisconnect as e:
        print(f"Calling interface WebSocket disconnected with code: {e.code}")
        # Clean up the WebSocket registration
        if current_call_sid:
            manager.cleanup_call(current_call_sid)
    except json.JSONDecodeError as e:
        print(f"Failed to parse message as JSON: {e}")
    except Exception as e:
        print(f"WebSocket error: {e}")
        import traceback
        traceback.print_exc()
        # Clean up the WebSocket registration
        if current_call_sid:
            manager.cleanup_call(current_call_sid)

    print("Calling interface WebSocket disconnected")


@app.websocket("/media-stream")
async def media_stream_endpoint(websocket: WebSocket):
    """WebSocket endpoint for Twilio Media Streams"""

    call_sid = None
    stream_sid = None
    handler = None
    print("\nMedia stream WebSocket endpoint called")

    try:
        # Accept the WebSocket connection
        await websocket.accept()
        print("Media stream WebSocket accepted, waiting for messages...")

        connected_received = False

        while True:
            # Receive messages in a loop
            try:
                message_text = await websocket.receive_text()
                message = json.loads(message_text)
                print(f"media_stream_endpoint message received: {message}")
            except asyncio.TimeoutError:
                print("Timeout waiting for message")
                continue
            except json.JSONDecodeError as e:
                print(f"Failed to parse message as JSON: {e}")
                continue

            event = message.get("event")

            if event == "connected":
                print("Received connected event - media stream WebSocket connection established")
                connected_received = True
                # Send acknowledgment if needed
                continue

            elif event == "start":
                if not connected_received:
                    print("Warning: Received start event before connected event")

                start_data = message.get("start", {})
                stream_sid = start_data.get("streamSid")
                call_sid = start_data.get("callSid")

                print(f"Received start message: call_sid={call_sid}, stream_sid={stream_sid}")

                # Check if this is for an outbound call
                if call_sid and call_sid in manager.call_sessions:
                    # This is an outbound call - use existing handler if available
                    handler = manager.get_handler_for_call(call_sid)
                    if handler:
                        print(f"Using existing handler for outbound call: {call_sid}")
                        # Update the handler's websocket
                        handler.twilio_websocket = websocket
                    else:
                        print(f"Creating new handler for outbound call: {call_sid}")
                        handler = await manager.new_session(websocket, call_sid)
                else:
                    # This is an inbound call
                    print("Creating handler for inbound call")
                    handler = await manager.new_session(websocket, call_sid)

                if handler:
                    # Register the handler for this stream
                    if stream_sid:
                        manager.register_stream_handler(stream_sid, handler)

                    # Start the handler
                    print("Starting handler session...")
                    await handler.start()
                    print("Handler started successfully")

                    # Notify the calling interface that media stream is connected
                    if call_sid:
                        await manager.notify_media_stream_connected(call_sid)

                    # Handler is now running and will take over the WebSocket
                    # The media stream endpoint should exit and let the handler handle all subsequent messages
                    print("Handler started, media stream endpoint exiting to let handler take over...")
                    return

            elif event == "media":
                # If we receive media before start, something is wrong
                print("Warning: Received media message before start event - this shouldn't happen")

            elif event == "stop" or event == "mark":
                # If we receive other events before start, log them
                print(f"Received {event} event before start - ignoring")

            else:
                print(f"Received unhandled event before start: {event}")

    except WebSocketDisconnect as e:
        print(f"WebSocket disconnected for call_sid={call_sid}, stream_sid={stream_sid} with code: {e.code}")
    except Exception as e:
        print(f"WebSocket error for call_sid={call_sid}, stream_sid={stream_sid}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Cleanup
        print(f"Cleaning up resources for call_sid={call_sid}, stream_sid={stream_sid}")
        if call_sid and stream_sid:
            manager.cleanup_call(call_sid, stream_sid)
        elif call_sid:
            manager.cleanup_call(call_sid)
        print("Media stream endpoint cleanup completed")


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 3000))  # Changed default to 3000 to match ngrok
    uvicorn.run(app, host="0.0.0.0", port=port)
# i am getting this error WebSocket error eee: (<CloseCode.NO_STATUS_RCVD: 1005>, '') and also we have to do converstion properloy  not we have to say one masage and liste the user input etc like we defin in the media_stream_endpoint 