import asyncio
import json
import logging
import uuid
import time
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MockServer")

# Global In-Memory State
processed_request_ids = set()
stored_messages = []
chaos_config = {
    "force_disconnect_ws": False,
    "latency_ms": 0,
    "drop_next_n_ws_messages": 0
}

routes = web.RouteTableDef()

# --- REST ENDPOINTS ---

@routes.post('/api/v1/auth/login')
async def handle_login(request):
    data = await request.json()
    username = data.get("username", "user")
    logger.info(f"REST: Login request for user={username}")
    return web.json_response({
        "status": "success",
        "auth_token": f"mock_token_{uuid.uuid4().hex[:8]}",
        "user_id": f"usr_{username}",
        "device_id": data.get("device_id", "dev_default")
    })

@routes.get('/api/v1/capabilities')
async def handle_capabilities(request):
    return web.json_response({
        "protocol_version": 1,
        "server_version": "1.0.0-mock",
        "features": {
            "websocket": True,
            "rest_fallback": True,
            "offline_backlog": True
        }
    })

@routes.get('/api/v1/sync/backlog')
async def handle_backlog(request):
    since_id = request.query.get("since_event_id", "")
    logger.info(f"REST: Backlog requested since_id='{since_id}'")
    
    # Return items after since_id or all if empty
    backlog = []
    found = False if since_id else True
    for msg in stored_messages:
        if found:
            backlog.append(msg)
        elif msg.get("envelope_id") == since_id:
            found = True
            
    return web.json_response({
        "status": "success",
        "backlog_items": backlog,
        "total_count": len(backlog)
    })

@routes.post('/api/v1/messages/send')
async def handle_rest_send(request):
    envelope = await request.json()
    req_id = envelope.get("client_request_id")
    
    logger.info(f"REST Fallback Send: req_id={req_id}")
    
    # Deduplication check
    if req_id in processed_request_ids:
        logger.warning(f"REST: Duplicate client_request_id={req_id} detected! Returning cached ACK.")
        return web.json_response({
            "status": "accepted",
            "duplicate": True,
            "envelope_id": envelope.get("envelope_id")
        })
        
    processed_request_ids.add(req_id)
    stored_messages.append(envelope)
    
    return web.json_response({
        "status": "accepted",
        "duplicate": False,
        "envelope_id": envelope.get("envelope_id"),
        "timestamp": int(time.time() * 1000)
    })

# Chaos Engineering Controls
@routes.post('/api/v1/chaos/configure')
async def handle_chaos(request):
    data = await request.json()
    chaos_config.update(data)
    logger.info(f"CHAOS CONFIG UPDATED: {chaos_config}")
    return web.json_response({"status": "updated", "config": chaos_config})


# --- WEBSOCKET ENDPOINT ---

@routes.get('/ws/v1/connect')
async def handle_websocket(request):
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    
    logger.info("WS: Client connected to WebSocket endpoint")
    authenticated = False
    client_device_id = "unknown"
    
    try:
        async for msg in ws:
            if chaos_config["force_disconnect_ws"]:
                logger.warning("CHAOS: Forcing WS disconnect!")
                await ws.close(code=1011, message=b"Chaos forced disconnect")
                break
                
            if chaos_config["latency_ms"] > 0:
                await asyncio.sleep(chaos_config["latency_ms"] / 1000.0)

            if msg.type == WSMsgType.TEXT:
                try:
                    envelope = json.loads(msg.data)
                except Exception as e:
                    logger.error(f"WS Malformed JSON: {e}")
                    await ws.send_json({
                        "protocol_version": 1,
                        "envelope_type": "error_event",
                        "envelope_id": str(uuid.uuid4()),
                        "timestamp": int(time.time() * 1000),
                        "payload_mode": "plain_json",
                        "payload": {"error_code": "MALFORMED_ENVELOPE", "message": str(e)}
                    })
                    continue

                env_type = envelope.get("envelope_type")
                env_id = envelope.get("envelope_id")
                req_id = envelope.get("client_request_id")
                
                logger.info(f"WS Received [{env_type}] env_id={env_id} req_id={req_id}")

                # 1. Client Hello (Handshake)
                if env_type == "client_hello":
                    payload = envelope.get("payload", {})
                    token = payload.get("auth_token", "")
                    client_device_id = envelope.get("sender_device_id", "dev_client")
                    
                    if not token or token == "invalid_token":
                        logger.warning("WS Auth Failed: Invalid token")
                        await ws.send_json({
                            "protocol_version": 1,
                            "envelope_type": "error_event",
                            "envelope_id": str(uuid.uuid4()),
                            "timestamp": int(time.time() * 1000),
                            "payload_mode": "plain_json",
                            "payload": {"error_code": "AUTH_EXPIRED", "message": "Token expired or invalid"}
                        })
                        await ws.close()
                        return
                    
                    authenticated = True
                    logger.info(f"WS Handshake SUCCESS for device={client_device_id}")
                    
                    # Send Server Hello
                    await ws.send_json({
                        "protocol_version": 1,
                        "envelope_type": "server_hello",
                        "envelope_id": str(uuid.uuid4()),
                        "timestamp": int(time.time() * 1000),
                        "payload_mode": "plain_json",
                        "payload": {
                            "session_id": f"sess_{uuid.uuid4().hex[:6]}",
                            "authenticated": True,
                            "heartbeat_interval_ms": 15000,
                            "has_unfetched_backlog": len(stored_messages) > 0,
                            "pending_events_count": len(stored_messages)
                        }
                    })

                # 2. Ping Keepalive
                elif env_type == "ping":
                    await ws.send_json({
                        "protocol_version": 1,
                        "envelope_type": "pong",
                        "envelope_id": str(uuid.uuid4()),
                        "timestamp": int(time.time() * 1000),
                        "payload_mode": "plain_json",
                        "payload": {}
                    })

                # 3. Message Send
                elif env_type == "message_send":
                    if not authenticated:
                        logger.warning("WS Error: Message sent before authentication")
                        await ws.send_json({
                            "protocol_version": 1,
                            "envelope_type": "error_event",
                            "envelope_id": str(uuid.uuid4()),
                            "timestamp": int(time.time() * 1000),
                            "payload_mode": "plain_json",
                            "payload": {"error_code": "UNAUTHORIZED", "message": "Send client_hello first"}
                        })
                        continue

                    # Check deduplication
                    is_dup = req_id in processed_request_ids
                    if not is_dup and req_id:
                        processed_request_ids.add(req_id)
                        stored_messages.append(envelope)

                    # Send ACK Event (Accepted by server)
                    ack_env = {
                        "protocol_version": 1,
                        "envelope_type": "ack_event",
                        "envelope_id": str(uuid.uuid4()),
                        "client_request_id": req_id,
                        "conversation_id": envelope.get("conversation_id"),
                        "sender_device_id": "server_node_1",
                        "recipient_device_id": client_device_id,
                        "timestamp": int(time.time() * 1000),
                        "payload_mode": "plain_json",
                        "payload": {
                            "target_envelope_id": env_id,
                            "status": "accepted",
                            "duplicate": is_dup
                        }
                    }
                    await ws.send_json(ack_env)

                    # Simulate server pushing delivery ACK after 500ms
                    async def async_delivery_push(target_env_id, r_id, conv_id):
                        await asyncio.sleep(0.5)
                        if not ws.closed:
                            deliv_env = {
                                "protocol_version": 1,
                                "envelope_type": "ack_event",
                                "envelope_id": str(uuid.uuid4()),
                                "client_request_id": r_id,
                                "conversation_id": conv_id,
                                "sender_device_id": "server_node_1",
                                "recipient_device_id": client_device_id,
                                "timestamp": int(time.time() * 1000),
                                "payload_mode": "plain_json",
                                "payload": {
                                    "target_envelope_id": target_env_id,
                                    "status": "delivered"
                                }
                            }
                            try:
                                await ws.send_json(deliv_env)
                            except Exception:
                                pass
                                
                    asyncio.create_task(async_delivery_push(env_id, req_id, envelope.get("conversation_id")))

            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                logger.info(f"WS Closed: {ws.close_code}")
                break

    except Exception as e:
        logger.error(f"WS Exception: {e}")
    finally:
        logger.info(f"WS Session ended for device={client_device_id}")
        
    return ws

def create_app():
    app = web.Application()
    app.add_routes(routes)
    return app

if __name__ == "__main__":
    app = create_app()
    logger.info("Starting Mock Transport Server on http://127.0.0.1:8080")
    web.run_app(app, host="127.0.0.1", port=8080)
