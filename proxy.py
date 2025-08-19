
import asyncio
import os
import logging
from typing import Dict, Any, List, Tuple, Optional
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dc_field
import yaml
from aiohttp import web
from pysolarmanv5 import PySolarmanV5Async, V5FrameError, NoSocketAvailableError

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("deye-proxy-http")

# ----------------- Environment -----------------
LOGGER_IP   = os.getenv("LOGGER_IP", "192.168.1.50")
LOGGER_SN   = int(os.getenv("LOGGER_SN", "1234567890"))
LOGGER_PORT = int(os.getenv("LOGGER_PORT", "8899"))
MB_SLAVE_ID = int(os.getenv("MB_SLAVE_ID", "1"))

SOCKET_TIMEOUT  = float(os.getenv("SOCKET_TIMEOUT", "3.0"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "3.0"))
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "2.0"))

LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8080"))

YAML_PATH   = os.getenv("REG_TABLE", "deye-modbus-registers.yaml")

# NEW: global rounding digits (max 2 decimals by default)
ROUND_DECIMALS = int(os.getenv("ROUND_DECIMALS", "2"))

# Circuit breaker
CB_FAIL_LIMIT   = int(os.getenv("CB_FAIL_LIMIT", "3"))
CB_OPEN_SECONDS = float(os.getenv("CB_OPEN_SECONDS", "30"))

# ----------------- Data structures -----------------

@dataclass
class RegSpec:
    id: str
    address: int
    count: int = 1
    func: str = "holding"              # "holding" or "input"
    dtype: str = "uint16"              # uint16,int16,uint32,int32
    byte_order: str = "AB"             # AB, ABCD, CDAB, DCBA
    multiply: float = 1.0              # scale factor
    offset: float = 0.0                # additive offset AFTER multiply
    measurement: str = "deye"
    field_name: str = ""               # output field name
    tags: Dict[str,str] = dc_field(default_factory=dict)

@dataclass
class AppState:
    regs: List[RegSpec] = dc_field(default_factory=list)
    cache: Dict[str, Dict[str, Any]] = dc_field(default_factory=dict)   # measurement -> fields
    cache_ts: float = 0.0
    cache_lock: asyncio.Lock = dc_field(default_factory=asyncio.Lock)
    sol: Optional[PySolarmanV5Async] = None
    breaker_failures: int = 0
    breaker_open_until: float = 0.0

STATE = AppState()

# ----------------- Helpers -----------------

def parse_yaml(path: str) -> List[RegSpec]:
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    items = []
    defaults = doc.get("defaults", {})
    for item in doc.get("registers", []):
        merged = {**defaults, **item}
        rs = RegSpec(
            id          = merged["id"],
            address     = int(merged["address"]),
            count       = int(merged.get("count", 1)),
            func        = str(merged.get("function", merged.get("func", "holding"))).lower(),
            dtype       = str(merged.get("type", merged.get("dtype","uint16"))).lower(),
            byte_order  = str(merged.get("byte_order", "AB")).upper(),
            multiply    = float(merged.get("multiply", merged.get("scale", 1.0))),
            offset      = float(merged.get("offset", 0.0)),
            measurement = str(merged.get("measurement", "deye")),
            field_name  = str(merged.get("field", merged.get("name", merged["id"]))),
            tags        = dict(merged.get("tags", {})),
        )
        items.append(rs)
    return items

def combine_words(words: List[int], dtype: str, byte_order: str) -> int:
    if len(words) == 1:
        val = words[0] & 0xFFFF
        if dtype == "int16":
            if val & 0x8000:
                val -= 0x10000
        return val

    if len(words) == 2:
        w0, w1 = words[0] & 0xFFFF, words[1] & 0xFFFF
        # Order by 16-bit WORDS:
        # ABCD -> w0 is high word, w1 is low word
        # CDAB -> w1 is high word, w0 is low word (Deye totals)
        if byte_order == "CDAB":
            hi, lo = w1, w0
        else:  # default ABCD
            hi, lo = w0, w1
        val = (hi << 16) | lo
        if dtype == "int32":
            if val & 0x80000000:
                val -= 0x100000000
        return val

    # Fallback: fold all words big-endian
    acc = 0
    for w in words:
        acc = (acc << 16) | (w & 0xFFFF)
    return acc

def apply_scale_offset(raw: float, multiply: float, offset: float) -> float:
    return float(raw) * float(multiply) + float(offset)

def merge_ranges(specs: List[RegSpec]) -> List[Tuple[int,int,List[RegSpec]]]:
    """Group contiguous (or near-contiguous) ranges to reduce read calls.
       Returns list of (start, qty, sublist) per function group.
    """
    MAX_QTY = 120
    out = []
    specs_sorted = sorted(specs, key=lambda s: s.address)
    i = 0
    while i < len(specs_sorted):
        start = specs_sorted[i].address
        end = start + specs_sorted[i].count - 1
        group = [specs_sorted[i]]
        i += 1
        while i < len(specs_sorted):
            nxt = specs_sorted[i]
            nxt_end = nxt.address + nxt.count - 1
            if nxt.address <= end + 2 and (nxt_end - start + 1) <= MAX_QTY:
                end = max(end, nxt_end)
                group.append(nxt)
                i += 1
            else:
                break
        qty = end - start + 1
        out.append((start, qty, group))
    return out

async def read_plan(client: PySolarmanV5Async, plan: List[Tuple[int,int,List[RegSpec]]], func: str, req_timeout: float) -> Dict[int,int]:
    """Execute grouped reads, return address->word map."""
    addr2word: Dict[int,int] = {}
    for start, qty, group in plan:
        try:
            if func == "holding":
                regs = await asyncio.wait_for(client.read_holding_registers(start, qty), timeout=req_timeout)
            else:
                regs = await asyncio.wait_for(client.read_input_registers(start, qty), timeout=req_timeout)
        except asyncio.TimeoutError:
            raise
        for idx, val in enumerate(regs):
            addr2word[start + idx] = int(val) & 0xFFFF
    return addr2word

async def refresh_cache() -> None:
    now_loop = asyncio.get_event_loop().time()
    if STATE.breaker_open_until > now_loop:
        return

    holding = [s for s in STATE.regs if s.func == "holding"]
    inputr  = [s for s in STATE.regs if s.func == "input"]
    plans = []
    if holding:
        plans.append(("holding", merge_ranges(holding)))
    if inputr:
        plans.append(("input", merge_ranges(inputr)))

    try:
        if STATE.sol is None:
            STATE.sol = PySolarmanV5Async(LOGGER_IP, LOGGER_SN, port=int(LOGGER_PORT),
                                          mb_slave_id=int(MB_SLAVE_ID),
                                          socket_timeout=SOCKET_TIMEOUT,
                                          auto_reconnect=True,
                                          verbose=False)
            await STATE.sol.connect()

        all_words: Dict[str,Dict[int,int]] = {}
        for func, plan in plans:
            all_words[func] = await read_plan(STATE.sol, plan, func, REQUEST_TIMEOUT)

        new_cache: Dict[str, Dict[str, Any]] = {}
        for spec in STATE.regs:
            source = all_words["holding"] if spec.func == "holding" else all_words["input"]
            words = [source[spec.address + off] for off in range(spec.count)]
            raw = combine_words(words, spec.dtype, spec.byte_order)
            val = apply_scale_offset(raw, spec.multiply, spec.offset)
            # round to at most N decimals (default 2)
            val = round(val, ROUND_DECIMALS)
            m = new_cache.setdefault(spec.measurement, {})
            m[spec.field_name or spec.id] = val

        async with STATE.cache_lock:
            STATE.cache = new_cache
            STATE.cache_ts = asyncio.get_event_loop().time()
            STATE.breaker_failures = 0
        log.debug("Cache refreshed")

    except (V5FrameError, NoSocketAvailableError, asyncio.TimeoutError, KeyError, OSError) as e:
        log.warning(f"refresh_cache error: {e}")
        STATE.breaker_failures += 1
        if STATE.breaker_failures >= CB_FAIL_LIMIT:
            STATE.breaker_open_until = now_loop + CB_OPEN_SECONDS
            log.error(f"Circuit breaker OPEN for {CB_OPEN_SECONDS}s after {STATE.breaker_failures} failures. Serving stale cache.")

# ----------------- HTTP Handlers -----------------

async def handle_get(request: web.Request) -> web.Response:
    # NIENTE refresh qui: serviamo esclusivamente lo snapshot in cache
    async with STATE.cache_lock:
        payload: List[Dict[str, Any]] = []
        for meas, fields in STATE.cache.items():
            obj = {"name": meas}
            obj.update(fields)
            payload.append(obj)
    return web.json_response(payload)

async def handle_health(request: web.Request) -> web.Response:
    info = {
        "cache_age_s": (asyncio.get_event_loop().time() - STATE.cache_ts) if STATE.cache_ts else None,
        "cache_ts": STATE.cache_ts,
        "breaker_failures": STATE.breaker_failures,
        "breaker_open_until": STATE.breaker_open_until,
        "regs_loaded": len(STATE.regs),
        "round_decimals": ROUND_DECIMALS,
    }
    return web.json_response(info)

# ----------------- Startup -----------------

async def background_worker(app: web.Application):
    STATE.regs = parse_yaml(YAML_PATH)
    log.info(f"Loaded {len(STATE.regs)} register specs from {YAML_PATH}")
    try:
        STATE.sol = PySolarmanV5Async(LOGGER_IP, LOGGER_SN, port=int(LOGGER_PORT),
                                      mb_slave_id=int(MB_SLAVE_ID),
                                      socket_timeout=SOCKET_TIMEOUT,
                                      auto_reconnect=True, verbose=False)
        await STATE.sol.connect()
    except Exception as e:
        log.warning(f"Initial connect failed: {e} (will retry in background)")

    while True:
        try:
            await refresh_cache()
        except Exception as e:
            log.error(f"Background refresh error: {e}")
        await asyncio.sleep(POLL_INTERVAL_S)

async def on_startup(app: web.Application):
    app["bg_task"] = asyncio.create_task(background_worker(app))

async def on_cleanup(app: web.Application):
    task = app.get("bg_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if STATE.sol:
        try:
            await STATE.sol.disconnect()
        except Exception:
            pass

def main():
    app = web.Application()
    app.router.add_get("/deye-registers", handle_get)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT)

if __name__ == "__main__":
    main()
