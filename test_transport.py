import asyncio
import sys
import os
import logging
import aiohttp

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_server.server import create_app
from transport_sdk.network_client import NetworkClient, ConnectionState

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [TEST] %(message)s")
logger = logging.getLogger("TransportTest")

async def run_integration_tests():
    logger.info("=== STARTING INTEGRATION TESTS FOR TRANSPORT LAYER ===")

    # 1. Start Mock Server
    app = create_app()
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, '127.0.0.1', 8088)
    await site.start()
    logger.info("Mock Server running on http://127.0.0.1:8088")

    received_acks = []
    received_messages = []
    state_transitions = []

    try:
        # 2. Instantiate NetworkClient
        client = NetworkClient(
            base_url="http://127.0.0.1:8088",
            ws_url="http://127.0.0.1:8088",
            device_id="test_device_alpha"
        )

        def on_state(old_s, new_s):
            state_transitions.append(new_s)
            logger.info(f"[TEST EVENT] State changed: {old_s.value} -> {new_s.value}")

        def on_ack(ack_env):
            received_acks.append(ack_env)
            payload = ack_env.get("payload", {})
            logger.info(f"[TEST EVENT] ACK received! status={payload.get('status')} target_id={payload.get('target_envelope_id')}")

        def on_msg(msg_env):
            received_messages.append(msg_env)
            logger.info(f"[TEST EVENT] Message push received! id={msg_env.get('envelope_id')}")

        client.on_state_change = on_state
        client.on_ack_received = on_ack
        client.on_message_received = on_msg

        # TEST 1: REST Login
        logger.info("--- TEST 1: REST Login ---")
        login_ok = await client.login("engineer_client")
        assert login_ok is True, "Login failed!"
        assert client.auth_token is not None, "Auth token is None!"
        logger.info("TEST 1 PASSED: REST Login successful.")

        # TEST 2: WS Connect & Handshake
        logger.info("--- TEST 2: WS Connection & Handshake ---")
        await client.start()
        await asyncio.sleep(1.0)
        assert client.state == ConnectionState.CONNECTED, f"Expected CONNECTED state, got {client.state}"
        logger.info("TEST 2 PASSED: Client is in CONNECTED state.")

        # TEST 3: Send Live WS Message & Receive ACKs
        logger.info("--- TEST 3: Live Send & ACKs ---")
        res = await client.send_message(
            conversation_id="chat_general",
            payload={"text": "Hello world from transport test!"},
            payload_mode="plain_json"
        )
        assert res["status"] == "sent_ws", f"Expected sent_ws, got {res['status']}"

        # Wait for accepted & delivered ACKs
        await asyncio.sleep(1.5)
        assert len(received_acks) >= 2, f"Expected at least 2 ACKs (accepted, delivered), got {len(received_acks)}"
        statuses = [a["payload"]["status"] for a in received_acks]
        assert "accepted" in statuses, "Missing 'accepted' ACK!"
        assert "delivered" in statuses, "Missing 'delivered' ACK!"
        logger.info(f"TEST 3 PASSED: ACKs received successfully: {statuses}")

        # TEST 4: Offline Queue & Auto-Flush on Reconnect
        logger.info("--- TEST 4: Offline Queue & Auto-Flush ---")
        # Simulate Offline state
        client.state = ConnectionState.OFFLINE
        res_off = await client.send_message(
            conversation_id="chat_general",
            payload={"text": "Queued while offline!"},
            payload_mode="plain_json"
        )
        assert res_off["status"] == "queued_offline", "Failed to queue message while offline!"
        assert len(client.pending_queue) == 1, "Pending queue count should be 1!"
        logger.info("Message correctly queued in PendingQueue.")

        # Restore state and flush queue
        client.state = ConnectionState.CONNECTED
        await client.flush_pending_queue()
        await asyncio.sleep(1.0)
        assert len(client.pending_queue) == 0, "PendingQueue should be empty after flush!"
        logger.info("TEST 4 PASSED: PendingQueue successfully flushed and cleared.")

        # TEST 5: Graceful Shutdown
        logger.info("--- TEST 5: Graceful Shutdown ---")
        await client.stop()
        assert client.state == ConnectionState.DISCONNECTED, "Client should be DISCONNECTED!"
        logger.info("TEST 5 PASSED: Client stopped cleanly.")

        logger.info("\n==================================================")
        logger.info("ALL INTEGRATION TESTS PASSED SUCCESSFULLY! (5/5)")
        logger.info("==================================================\n")

    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(run_integration_tests())
