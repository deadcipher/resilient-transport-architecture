# Wire Protocol Specification v1.0

Данный документ определяет формальный контракт обмена сообщениями и фреймами между клиентом и сервером на транспортном уровне.

---

## 1. Базовый конверт (WireEnvelope)

Каждое сообщение (REST payload или WebSocket фрейм) передается внутри строго фиксированного конверта `WireEnvelope`. Транспортный слой работает исключительно с этим конвертом и **не анализирует содержимого `payload`**.

### JSON Schema (WireEnvelope)

```json
{
  "protocol_version": 1,
  "envelope_type": "message_send | message_push | ack_event | read_event | client_hello | server_hello | ping | pong | error_event",
  "envelope_id": "uuid-v4-string",
  "client_request_id": "uuid-v4-string-or-null",
  "conversation_id": "string-id-or-null",
  "sender_device_id": "string-device-id",
  "recipient_device_id": "string-device-id-or-null",
  "timestamp": 1740000000000,
  "payload_mode": "encrypted_blob | plain_json | raw_binary",
  "payload": {},
  "trace_id": "opt-trace-uuid"
}
```

### Поля конверта

| Поле | Тип | Описание | Обязательность |
| :--- | :--- | :--- | :--- |
| `protocol_version` | `uint32` | Версия протокола (на текущий момент `1`) | Да |
| `envelope_type` | `string` | Тип транспортного фрейма | Да |
| `envelope_id` | `UUIDv4` | Уникальный ID сетевого конверта | Да |
| `client_request_id` | `UUIDv4` | Ключ идемпотентности, генерируемый клиентом | Для отправки/ACK |
| `conversation_id` | `string` | Идентификатор диалога/чата | Опционально |
| `sender_device_id` | `string` | Идентификатор устройства отправителя | Да |
| `recipient_device_id`| `string` | Идентификатор конкретного устройства получателя | Опционально |
| `timestamp` | `int64` | Unix Epoch timestamp в миллисекундах | Да |
| `payload_mode` | `enum` | Формат содержимого (`encrypted_blob`, `plain_json`, `raw_binary`) | Да |
| `payload` | `object/string` | Содержимое (зашифрованное или системное) | Да |

---

## 2. Типы фреймов (Envelope Types)

### 2.1 Handshake (`client_hello` / `server_hello`)

#### `client_hello` (Отправляется клиентом сразу после поднятия WebSocket)
```json
{
  "protocol_version": 1,
  "envelope_type": "client_hello",
  "envelope_id": "c3d9a101-8b2a-4a21-99ef-111111111111",
  "sender_device_id": "device_ios_001",
  "timestamp": 1740000000100,
  "payload_mode": "plain_json",
  "payload": {
    "auth_token": "bearer_jwt_or_session_token",
    "last_received_event_id": "evt_9948102",
    "client_version": "1.0.0",
    "device_info": "iOS 18.1 / iPhone 16 Pro"
  }
}
```

#### `server_hello` (Ответ сервера при успешной авторизации WS-сессии)
```json
{
  "protocol_version": 1,
  "envelope_type": "server_hello",
  "envelope_id": "s8e1f202-9a3b-5b32-00ff-222222222222",
  "timestamp": 1740000000150,
  "payload_mode": "plain_json",
  "payload": {
    "session_id": "ws_sess_777123",
    "authenticated": true,
    "user_id": "user_404",
    "heartbeat_interval_ms": 15000,
    "has_unfetched_backlog": true,
    "pending_events_count": 5
  }
}
```

---

### 2.2 Сообщения и подтверждения (ACK / Delivery / Read)

#### `ack_event` (Сетевое подтверждение статуса)
```json
{
  "protocol_version": 1,
  "envelope_type": "ack_event",
  "envelope_id": "a1b2c3d4-0000-1111-2222-333333333333",
  "client_request_id": "req_881231",
  "conversation_id": "chat_9912",
  "sender_device_id": "server_node_1",
  "recipient_device_id": "device_ios_001",
  "timestamp": 1740000000250,
  "payload_mode": "plain_json",
  "payload": {
    "target_envelope_id": "msg_envelope_9012",
    "status": "accepted | delivered | read | failed",
    "error_code": null
  }
}
```

---

### 2.3 Heartbeat (`ping` / `pong`)

```json
// Ping от клиента
{
  "protocol_version": 1,
  "envelope_type": "ping",
  "envelope_id": "p111-ping",
  "timestamp": 1740000015000,
  "payload_mode": "plain_json",
  "payload": {}
}

// Pong от сервера
{
  "protocol_version": 1,
  "envelope_type": "pong",
  "envelope_id": "p222-pong",
  "timestamp": 1740000015020,
  "payload_mode": "plain_json",
  "payload": {}
}
```

---

### 2.4 Сетевые ошибки (`error_event`)

```json
{
  "protocol_version": 1,
  "envelope_type": "error_event",
  "envelope_id": "err_001928",
  "timestamp": 1740000000500,
  "payload_mode": "plain_json",
  "payload": {
    "error_code": "AUTH_EXPIRED | RATE_LIMITED | MALFORMED_ENVELOPE | PROTOCOL_VERSION_MISMATCH | SERVER_SHUTTING_DOWN",
    "message": "Token expired, re-authentication required",
    "retry_after_ms": 5000,
    "fatal": false
  }
}
```

---

## 3. Правила версионирования и эволюции

1. **Неизменяемость Envelope:** Мажорные поля (`protocol_version`, `envelope_type`, `envelope_id`) не могут менять свои имена или типы.
2. **Игнорирование неизвестных полей:** При получении фрейма с неизвестными доп. полями клиент и сервер **обязаны игнорировать** эти поля без паники (Forward Compatibility).
3. **Версионирование:** При расхождении `protocol_version` сервер отправляет `error_event` с кодом `PROTOCOL_VERSION_MISMATCH` и списком поддерживаемых версий.
