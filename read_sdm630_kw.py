#!/usr/bin/env python3
"""Proof-of-concept SDM630 Modbus TCP power reader.

Register reference:
  Eastron SDM630 Modbus Protocol V1.8, section 1.2.1 Input Registers.
  "Total system power" is input register 30053, Modbus start address 0x0034,
  units Watts, function code 04, quantity 2 registers.

The SDM630 returns each measurement as a 32-bit IEEE754 float using two
registers. The default register order is most significant register first.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys


DEFAULT_HOST = "10.201.150.254"
DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 2

FUNCTION_READ_INPUT_REGISTERS = 0x04
TOTAL_SYSTEM_POWER_START = 0x0034
REGISTER_QUANTITY = 2


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed before full response was received")
        data.extend(chunk)
    return bytes(data)


def read_total_system_power_watts(
    host: str,
    port: int,
    unit_id: int,
    timeout: float,
    reverse_register_order: bool,
) -> float:
    transaction_id = 1
    protocol_id = 0
    length = 6  # Unit id byte + function + start address + register quantity.

    request = struct.pack(
        ">HHHBBHH",
        transaction_id,
        protocol_id,
        length,
        unit_id,
        FUNCTION_READ_INPUT_REGISTERS,
        TOTAL_SYSTEM_POWER_START,
        REGISTER_QUANTITY,
    )

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request)

        header = recv_exact(sock, 7)
        response_transaction_id, response_protocol_id, response_length, response_unit_id = (
            struct.unpack(">HHHB", header)
        )
        body = recv_exact(sock, response_length - 1)

    if response_transaction_id != transaction_id:
        raise RuntimeError("unexpected Modbus transaction id in response")
    if response_protocol_id != protocol_id:
        raise RuntimeError("unexpected Modbus protocol id in response")
    if response_unit_id != unit_id:
        raise RuntimeError("unexpected Modbus unit id in response")
    if len(body) < 2:
        raise RuntimeError("short Modbus response")

    function_code = body[0]
    if function_code == (FUNCTION_READ_INPUT_REGISTERS | 0x80):
        exception_code = body[1]
        raise RuntimeError(f"Modbus exception response: {exception_code}")
    if function_code != FUNCTION_READ_INPUT_REGISTERS:
        raise RuntimeError(f"unexpected Modbus function code: {function_code}")

    byte_count = body[1]
    register_bytes = body[2 : 2 + byte_count]
    if byte_count != 4 or len(register_bytes) != 4:
        raise RuntimeError(f"unexpected register byte count: {byte_count}")

    if reverse_register_order:
        register_bytes = register_bytes[2:4] + register_bytes[0:2]

    return struct.unpack(">f", register_bytes)[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read current kW from an SDM630 meter.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Modbus TCP host/IP")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="Modbus TCP port")
    parser.add_argument("--unit-id", default=DEFAULT_UNIT_ID, type=int, help="Modbus unit id")
    parser.add_argument("--timeout", default=3.0, type=float, help="Socket timeout in seconds")
    parser.add_argument(
        "--reverse-register-order",
        action="store_true",
        help="Use if the meter was configured for reversed float register order",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        watts = read_total_system_power_watts(
            host=args.host,
            port=args.port,
            unit_id=args.unit_id,
            timeout=args.timeout,
            reverse_register_order=args.reverse_register_order,
        )
    except Exception as exc:
        print(f"device: offline ({exc})")
        return 1

    print("device: online")
    print(f"current_kw: {watts / 1000:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
