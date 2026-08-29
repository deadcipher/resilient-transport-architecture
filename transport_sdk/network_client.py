import asyncio
import json
import logging
import random
import time
import uuid
from enum import Enum
from typing import Callable, Optional, Dict, Any, List
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [ClientSDK] %(message)s")
logger = logging.getLogger("NetworkClient")

class ConnectionState(Enum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATED = "AUTHENTICATED"
    SYNCING_BACKLOG = "SYNCING_BACKLOG"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    OFFLINE = "OFFLINE"

class NetworkClient:
    def __init__(self, base_url: str = "", ws_url: str = "", device_id: str = ""):
        self.base_url = base_url.rstrip('/')
        self.ws_url = ws_url.rstrip('/')
        self.device_id = device_id or f"device_{uuid.uuid4().hex[:6]}"
        self.auth_token: Optional[str] = None
        
        self.state: ConnectionState = ConnectionState.NOT_CONFIGURED if not base_url else ConnectionState.DISCONNECTED
        
        # Event Callbacks
        self.on_state_change: Optional[Callable[[ConnectionState, ConnectionState], None]] = None
        self.on_message_received: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_ack_received: Optional[Callable[[Dict[str, Any]], None]] = None

        # Queue & Backlog
        self.pending_queue: List[Dict[str, Any]] = []
        self.last_received_event_id: Optional[str] = None
        
        # Connection Internals
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        
        # Reconnect parameters
        self._reconnect_attempt = 0
        self._base_backoff = 1.0
        self._max_backoff = 30.0
        self._last_pong_time = time.time()
        self._running = False

    def configure(self, base_url: str, ws_url: str, device_id: str = ""):
        self.base_url = base_url.rstrip('/')
        self.ws_url = ws_url.rstrip('/')
        if device_id:
            self.device_id = device_id
        self._set_state(ConnectionState.DISCONNECTED)

    def _set_state(self, new_state: ConnectionState):
        if self.state != new_state:
            logger.info(f"State transition: {self.state.value} ---> {new_state.value}")
            old_state = self.state
            self.state = new_state
            if self.on_state_change:
                try:
                    self.on_state_change(old_state, new_state)
                except Exception as e:
                    logger.error(f"Error in on_state_change callback: {e}")

    async def login(self, username: str) -> bool:
        """REST login to obtain session token"""
        if not self.base_url:
            raise ValueError("Base URL not configured")
            
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/api/v1/auth/login"
            try:
                async with session.post(url, json={"username": username, "device_id": self.device_id}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self.auth_token = data.get("auth_token")
                        logger.info(f"REST Login successful, token={self.auth_token}")
                        return True
                    else:
                        logger.error(f"REST Login failed: HTTP {resp.status}")
                        return False
            except Exception as e:
                logger.error(f"REST Login exception: {e}")
                return False

    async def start(self):
        """Start the background connection & heartbeat loops"""
        if not self.auth_token:
            logger.error("Cannot start NetworkClient without auth_token")
            return
            
        self._running = True
        self._session = aiohttp.ClientSession()
        self._loop_task = asyncio.create_task(self._connection_loop())

    async def stop(self):
        """Gracefully stop the client"""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        if self._loop_task:
            self._loop_task.cancel()
        self._set_state(ConnectionState.DISCONNECTED)
        logger.info("NetworkClient stopped")

    async def _connection_loop(self):
        while self._running:
            try:
                self._set_state(ConnectionState.CONNECTING)
                ws_endpoint = f"{self.ws_url}/ws/v1/connect"
                logger.info(f"Attempting WS connection to {ws_endpoint}...")
                
                async with self._session.ws_connect(ws_endpoint) as ws:
                    self._ws = ws
                    self._reconnect_attempt = 0
                    
                    # 1. Perform Handshake (client_hello)
                    client_hello = {
                        "protocol_version": 1,
                        "envelope_type": "client_hello",
                        "envelope_id": str(uuid.uuid4()),
                        "sender_device_id": self.device_id,
                        "timestamp": int(time.time() * 1000),
                        "payload_mode": "plain_json",
                        "payload": {
                            "auth_token": self.auth_token,
                            "last_received_event_id": self.last_received_event_id
                        }
                    }
                    await ws.send_json(client_hello)
                    
                    # Start Heartbeat loop
                    self._last_pong_time = time.time()
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                    # Read incoming WS frames
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_ws_frame(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"WS Connection closed: {ws.close_code}")
                            break

            except Exception as e:
                logger.warning(f"WS Connection error: {e}")
                
            finally:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                self._ws = None

            if not self._running:
                break

            # Handle Reconnect Backoff with Full Jitter
            self._set_state(ConnectionState.RECONNECTING)
            self._reconnect_attempt += 1
            temp = min(self._max_backoff, self._base_backoff * (2 ** (self._reconnect_attempt - 1)))
            sleep_duration = random.uniform(0, temp)
            logger.info(f"Reconnect attempt #{self._reconnect_attempt} in {sleep_duration:.2f}s (backoff max={temp:.2f}s)")
            await asyncio.sleep(sleep_duration)

    async def _handle_ws_frame(self, envelope: Dict[str, Any]):
        env_type = envelope.get("envelope_type")
        env_id = envelope.get("envelope_id")
        
        logger.info(f"Received WS Envelope: [{env_type}] env_id={env_id}")
        
        if env_type == "server_hello":
            self._set_state(ConnectionState.AUTHENTICATED)
            payload = envelope.get("payload", {})
            has_backlog = payload.get("has_unfetched_backlog", False)
            
            if has_backlog:
                self._set_state(ConnectionState.SYNCING_BACKLOG)
                await self._fetch_backlog()
                
            self._set_state(ConnectionState.CONNECTED)
            # Flush any queued offline messages
            asyncio.create_task(self.flush_pending_queue())

        elif env_type == "pong":
            self._last_pong_time = time.time()

        elif env_type == "ack_event":
            if self.on_ack_received:
                self.on_ack_received(envelope)

        elif env_type == "message_push":
            self.last_received_event_id = env_id
            if self.on_message_received:
                self.on_message_received(envelope)

        elif env_type == "error_event":
            payload = envelope.get("payload", {})
            err_code = payload.get("error_code")
            logger.error(f"Received ErrorEnvelope from server: {err_code} - {payload.get('message')}")
            if err_code == "AUTH_EXPIRED":
                self._set_state(ConnectionState.DISCONNECTED)
                self._running = False

    async def _heartbeat_loop(self):
        while self._running and self._ws and not self._ws.closed:
            await asyncio.sleep(15.0)
            # Check Pong Timeout (Hard timeout 20s)
            if time.time() - self._last_pong_time > 20.0:
                logger.warning("Heartbeat PONG timeout (>20s)! Force closing WS socket...")
                if self._ws:
                    await self._ws.close()
                break

            ping_env = {
                "protocol_version": 1,
                "envelope_type": "ping",
                "envelope_id": str(uuid.uuid4()),
                "timestamp": int(time.time() * 1000),
                "payload_mode": "plain_json",
                "payload": {}
            }
            try:
                if self._ws:
                    await self._ws.send_json(ping_env)
            except Exception as e:
                logger.error(f"Error sending Ping: {e}")

    async def _fetch_backlog(self):
        """Fetch missed offline messages via REST"""
        url = f"{self.base_url}/api/v1/sync/backlog"
        params = {}
        if self.last_received_event_id:
            params["since_event_id"] = self.last_received_event_id

        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("backlog_items", [])
                    logger.info(f"Backlog sync retrieved {len(items)} items")
                    for item in items:
                        if self.on_message_received:
                            self.on_message_received(item)
                        self.last_received_event_id = item.get("envelope_id")
        except Exception as e:
            logger.error(f"Failed to fetch backlog: {e}")

    async def send_message(self, conversation_id: str, payload: Any, payload_mode: str = "encrypted_blob") -> Dict[str, Any]:
        """Send message through active WS, REST fallback, or PendingQueue"""
        client_req_id = str(uuid.uuid4())
        env_id = str(uuid.uuid4())

        envelope = {
            "protocol_version": 1,
            "envelope_type": "message_send",
            "envelope_id": env_id,
            "client_request_id": client_req_id,
            "conversation_id": conversation_id,
            "sender_device_id": self.device_id,
            "timestamp": int(time.time() * 1000),
            "payload_mode": payload_mode,
            "payload": payload
        }

        if self.state == ConnectionState.CONNECTED and self._ws and not self._ws.closed:
            logger.info(f"Sending envelope via WS: req_id={client_req_id}")
            await self._ws.send_json(envelope)
            return {"status": "sent_ws", "envelope": envelope}

        elif self.state == ConnectionState.DEGRADED:
            logger.info(f"Sending envelope via REST Fallback: req_id={client_req_id}")
            url = f"{self.base_url}/api/v1/messages/send"
            async with self._session.post(url, json=envelope) as resp:
                data = await resp.json()
                return {"status": "sent_rest", "response": data, "envelope": envelope}

        else:
            logger.info(f"Client state is {self.state.value}. Enqueuing to PendingQueue: req_id={client_req_id}")
            self.pending_queue.append(envelope)
            return {"status": "queued_offline", "envelope": envelope}

    async def flush_pending_queue(self):
        """Flush queued offline messages once reconnected"""
        if not self.pending_queue:
            return

        logger.info(f"Flushing PendingQueue: {len(self.pending_queue)} messages to send...")
        items_to_flush = list(self.pending_queue)
        self.pending_queue.clear()

        for envelope in items_to_flush:
            if self.state == ConnectionState.CONNECTED and self._ws and not self._ws.closed:
                await self._ws.send_json(envelope)
                await asyncio.sleep(0.05)
            else:
                # Put back if dropped mid-flush
                self.pending_queue.append(envelope)
