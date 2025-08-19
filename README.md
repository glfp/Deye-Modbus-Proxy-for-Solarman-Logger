# Deye-Modbus-Proxy-for-Solarman-Logger

A tiny HTTP bridge that polls a Deye/Solarman logger over Modbus (via TCP/8899) and exposes the data as a simple JSON endpoint you can scrape from Telegraf, Grafana, or anything else.
It talks to the logger with `pysolarmanv5`, serves an HTTP API with `aiohttp`, and reads a **YAML register map** so you can define exactly which registers to fetch and how to scale them. Values are returned as floats and rounded (default: **2 decimals**).

---

## Why?

* **One clean HTTP endpoint** with **already-scaled** values (e.g., volts ×0.01, energy ×0.1). No Starlark or byte-order juggling in Telegraf—conversion happens in the proxy.
* **YAML-driven**: add/edit registers without touching code.
* **Background polling + in-memory snapshot**: the API never blocks on the logger; it serves the latest snapshot.
* **Circuit breaker**: if the logger misbehaves, the proxy keeps serving the last good snapshot.

---

## How it works

* A background worker polls the logger every `POLL_INTERVAL_S` seconds, groups Modbus reads, applies **type**, **byte order**, **multiply/offset**, then **rounds** to `ROUND_DECIMALS`.
  The result is stored as a **snapshot** in memory.
* The API returns the **snapshot** immediately. **No read-on-request**.
* If polling fails repeatedly, a **circuit breaker** stops hitting the logger for `CB_OPEN_SECONDS`, but the API continues to return the last good snapshot.

**Cache semantics you can rely on:**

* All requests that arrive within the same polling interval see **the same snapshot**.
* If a request hits while a refresh is in progress, you still get the **previous** snapshot (atomic swap when the new one is ready).
* On errors, the API stays up and serves **stale** data until the breaker closes.

---

## Endpoints

* `GET /deye-registers` → Array of measurement objects:

  ```json
  [
    { "name": "deye_battery", "battery_voltage_v": 54.37, "battery_soc_percent": 83 },
    { "name": "deye_pv", "pv1_power_w": 1875, "pv2_power_w": 1623 }
  ]
  ```

  Values are already scaled and rounded.

* `GET /health` → Basic status:

  ```json
  {
    "cache_age_s": 1.2,
    "regs_loaded": 42,
    "round_decimals": 2,
    "breaker_failures": 0
  }
  ```

**Example calls**

```bash
curl http://<PROXY_HOST>:8080/deye-registers
curl http://<PROXY_HOST>:8080/health
```

---

## The register map (`deye-modbus-registers.yaml`)

This file defines what to read and how to convert it.

```yaml
defaults:
  function: holding        # holding | input
  byte_order: AB           # 32-bit words ordering when count=2: ABCD, CDAB, etc.
  multiply: 1.0            # scale factor
  offset: 0.0              # add after scaling

registers:
  - id: deye_battery_voltage
    address: 587           # Modbus register (start)
    type: uint16           # uint16 | int16 | uint32 | int32
    multiply: 0.01
    measurement: deye_battery
    field: battery_voltage_v

  - id: deye_battery_total_discharge
    address: 518
    count: 2               # 32-bit value (two 16-bit words)
    type: uint32
    byte_order: CDAB       # many Deye totals are CDAB
    multiply: 0.1
    measurement: deye_energy
    field: battery_total_discharge_kwh
```

**Tips**

* Use `count: 2` for 32-bit registers and set `byte_order` as needed (`CDAB` is common on totals).
* Use `multiply`/`offset` to produce real-world units (e.g., volts ×0.01, °C via offset).
* The proxy returns floats rounded to `ROUND_DECIMALS` (default 2).

---

## Quick start

### Run locally (Python)

```bash
pip install -r requirements.txt
export LOGGER_IP=<LOGGER_IP> LOGGER_SN=<LOGGER_SN> LOGGER_PORT=8899 MB_SLAVE_ID=1
export REG_TABLE=./deye-modbus-registers.yaml
export POLL_INTERVAL_S=5 ROUND_DECIMALS=2
python proxy.py
# → http://0.0.0.0:8080/deye-registers
```

### Docker

Build from the provided `Dockerfile`:

```bash
docker build -t deye-logger-modbus-proxy .
```

Run with your YAML mounted in:

```bash
docker run --rm -p 8080:8080 \
  -e LOGGER_IP=<LOGGER_IP> \
  -e LOGGER_SN=<LOGGER_SN> \
  -e LOGGER_PORT=8899 \
  -e MB_SLAVE_ID=1 \
  -e POLL_INTERVAL_S=5 \
  -e ROUND_DECIMALS=2 \
  -e REG_TABLE=/app/deye-modbus-registers.yaml \
  -v $(pwd)/deye-modbus-registers.yaml:/app/deye-modbus-registers.yaml:ro \
  deye-logger-modbus-proxy
```

### Docker Compose (recommended)

Map your own YAML by mounting it as a volume:

```yaml
services:
  deye-proxy:
    image: deye-logger-modbus-proxy:latest
    ports:
      - "8080:8080"
    environment:
      LOGGER_IP: "<LOGGER_IP>"
      LOGGER_SN: "<LOGGER_SN>"
      LOGGER_PORT: "8899"
      MB_SLAVE_ID: "1"
      POLL_INTERVAL_S: "5"
      ROUND_DECIMALS: "2"
      REG_TABLE: "/app/deye-modbus-registers.yaml"
    volumes:
      - ./deye-modbus-registers.yaml:/app/deye-modbus-registers.yaml:ro
```

> **Personalize the YAML**: just edit your local `deye-modbus-registers.yaml`, then restart the container. No code changes required.

---

## Telegraf integration

Because the proxy returns an array where `"name"` is the measurement and the other keys are fields (already scaled/floats), the Telegraf config is simple:

```toml
[[inputs.http]]
  urls = ["http://<PROXY_HOST>:8080/deye-registers"]
  method = "GET"
  data_format = "json"
  json_name_key = "name"
  interval = "5s"
  timeout = "5s"
```

That’s it—no field-by-field conversions or byte-order handling in Telegraf.

---

## Configuration (environment variables)

* **Connection**

  * `LOGGER_IP` — logger IP
  * `LOGGER_SN` — logger serial number
  * `LOGGER_PORT` — TCP port (default `8899`)
  * `MB_SLAVE_ID` — Modbus slave id (default `1`)
* **Polling & timeouts**

  * `POLL_INTERVAL_S` — background refresh interval (e.g., `5`)
  * `SOCKET_TIMEOUT`, `REQUEST_TIMEOUT` — network/operation timeouts (seconds)
* **Rounding**

  * `ROUND_DECIMALS` — max decimals in output (default `2`)
* **YAML path**

  * `REG_TABLE` — path to `deye-modbus-registers.yaml`
* **Server**

  * `LISTEN_HOST` (default `0.0.0.0`), `LISTEN_PORT` (default `8080`)
* **Circuit breaker**

  * `CB_FAIL_LIMIT` (default `3`), `CB_OPEN_SECONDS` (default `30`)

---

## Example API usage

```bash
# All registers (snapshot)
curl -s http://<PROXY_HOST>:8080/deye-registers | jq .

# Health
curl -s http://<PROXY_HOST>:8080/health | jq .
```

Example response (abridged):

```json
[
  { "name": "deye_battery", "battery_voltage_v": 54.37, "battery_output_current_a": 8.88 },
  { "name": "deye_pv", "pv1_power_w": 1875, "pv2_power_w": 1623 },
  { "name": "deye_energy", "battery_total_discharge_kwh": 314.2 }
]
```

---

## Dependencies
[jmccrohan/pysolarmanv5](https://github.com/jmccrohan/pysolarmanv5/tree/main)
