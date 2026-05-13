#!/usr/bin/env python3
"""Proof-of-concept Growatt MAX inverter Modbus reader.

Register reference:
  Growatt MAC/MAX/MID Modbus RTU Protocol II V1.13.

  Section 4.2 Input Reg, first group:
    0       Inverter Status
    35-36   Pac H/L, Output power, U32, scale 0.1 W

This script uses Modbus TCP framing because the inverter is reached through a TCP
host/gateway. The Modbus unit id is the inverter address on the bus.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys


DEFAULT_HOST = "10.201.150.254"
DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 5
DEFAULT_UNIT_IDS = "1,3,4,5,6,7"

FUNCTION_READ_INPUT_REGISTERS = 0x04
INVERTER_STATUS_REGISTER = 0
OUTPUT_POWER_REGISTER = 35


def recv_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed before full response was received")
        data.extend(chunk)
    return bytes(data)


class ModbusTcpClient:
    def __init__(self, host: str, port: int, unit_id: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.transaction_id = 0

    def read_input_registers(self, start_address: int, quantity: int) -> list[int]:
        self.transaction_id = (self.transaction_id + 1) & 0xFFFF
        if self.transaction_id == 0:
            self.transaction_id = 1

        request = struct.pack(
            ">HHHBBHH",
            self.transaction_id,
            0,
            6,
            self.unit_id,
            FUNCTION_READ_INPUT_REGISTERS,
            start_address,
            quantity,
        )

        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.settimeout(self.timeout)
            sock.sendall(request)

            header = recv_exact(sock, 7)
            response_transaction_id, protocol_id, response_length, response_unit_id = (
                struct.unpack(">HHHB", header)
            )
            body = recv_exact(sock, response_length - 1)

        if response_transaction_id != self.transaction_id:
            raise RuntimeError("unexpected Modbus transaction id in response")
        if protocol_id != 0:
            raise RuntimeError("unexpected Modbus protocol id in response")
        if response_unit_id != self.unit_id:
            raise RuntimeError("unexpected Modbus unit id in response")
        if len(body) < 2:
            raise RuntimeError("short Modbus response")

        response_function_code = body[0]
        if response_function_code == (FUNCTION_READ_INPUT_REGISTERS | 0x80):
            exception_code = body[1]
            raise RuntimeError(f"Modbus exception response: {exception_code}")
        if response_function_code != FUNCTION_READ_INPUT_REGISTERS:
            raise RuntimeError(f"unexpected Modbus function code: {response_function_code}")

        byte_count = body[1]
        register_bytes = body[2 : 2 + byte_count]
        expected_byte_count = quantity * 2
        if byte_count != expected_byte_count or len(register_bytes) != expected_byte_count:
            raise RuntimeError(f"unexpected register byte count: {byte_count}")

        return list(struct.unpack(f">{quantity}H", register_bytes))


def unsigned_32_from_registers(registers: list[int]) -> int:
    if len(registers) != 2:
        raise ValueError("U32 values require exactly two registers")
    return (registers[0] << 16) | registers[1]


def status_label(status: int) -> str:
    labels = {
        0: "waiting",
        1: "normal",
        3: "fault",
    }
    return labels.get(status, f"unknown ({status})")


def read_inverter(host: str, port: int, unit_id: int, timeout: float) -> tuple[int, float]:
    client = ModbusTcpClient(host=host, port=port, unit_id=unit_id, timeout=timeout)

    status = client.read_input_registers(INVERTER_STATUS_REGISTER, 1)[0]
    power_registers = client.read_input_registers(OUTPUT_POWER_REGISTER, 2)

    # Pac H/L is an unsigned 32-bit value with 0.1 W resolution.
    production_kw = unsigned_32_from_registers(power_registers) / 10000
    return status, production_kw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read current production from a Growatt MAX inverter.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Modbus TCP host/IP")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="Modbus TCP port")
    parser.add_argument("--unit-id", default=DEFAULT_UNIT_ID, type=int, help="Modbus unit id / inverter address")
    parser.add_argument(
        "--unit-ids",
        help="Comma-separated unit ids to scan, for example: 1,3,4,5,6,7",
    )
    parser.add_argument("--timeout", default=3.0, type=float, help="Socket timeout in seconds")
    return parser.parse_args()


def parse_unit_ids(unit_ids: str) -> list[int]:
    return [int(value.strip()) for value in unit_ids.split(",") if value.strip()]


def print_inverter_reading(host: str, port: int, unit_id: int, timeout: float) -> bool:
    try:
        status, production_kw = read_inverter(
            host=host,
            port=port,
            unit_id=unit_id,
            timeout=timeout,
        )
    except Exception as exc:
        print(f"inverter {unit_id}: offline ({exc})")
        return False

    print(f"inverter {unit_id}: online")
    print(f"inverter {unit_id} status: {status_label(status)}")
    print(f"inverter {unit_id} current_production_kw: {production_kw:.3f}")
    return True


def main() -> int:
    args = parse_args()

    if args.unit_ids:
        unit_ids = parse_unit_ids(args.unit_ids)
        any_online = False
        for unit_id in unit_ids:
            any_online = print_inverter_reading(args.host, args.port, unit_id, args.timeout) or any_online
        return 0 if any_online else 1

    try:
        status, production_kw = read_inverter(
            host=args.host,
            port=args.port,
            unit_id=args.unit_id,
            timeout=args.timeout,
        )
    except Exception as exc:
        print(f"device: offline ({exc})")
        return 1

    print("device: online")
    print(f"inverter_status: {status_label(status)}")
    print(f"current_production_kw: {production_kw:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
