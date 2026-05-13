#!/usr/bin/env python3
"""Proof-of-concept CLOU ESS Modbus TCP battery reader.

Register reference:
  CLOU CI Energy Storage Modbus Communication Protocol C8 Version 250506 EN.

  Section 4/5:
    Standard Modbus TCP/IP, port 502, device address 0x01.
    Function code 03 reads holding registers / operation data.

  Register definition:
    0x6930       Total AC Output Active Power, S16, scale 10, unit kW
    0x6978       SOC of Energy Storage Cabinet, U16, scale 10, unit %
    0x6979       Charging and Discharging Status of Energy Storage Cabinet, U16
                 0 = Rest, 1 = Charging, 2 = Discharge

    Fallback string-level registers:
    0x5003       String SOC, U16, scale 10, unit %
    0x5005-5006  String Power, S32, scale 10, unit kW
    0x5054       Charging and Discharging Status, U16
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys


DEFAULT_HOST = "10.201.150.182"
DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 1

FUNCTION_READ_HOLDING_REGISTERS = 0x03
FUNCTION_READ_INPUT_REGISTERS = 0x04

TOTAL_AC_OUTPUT_ACTIVE_POWER_REGISTER = 0x6930
CABINET_SOC_REGISTER = 0x6978
CABINET_STATUS_REGISTER = 0x6979

STRING_SOC_REGISTER = 0x5003
STRING_POWER_REGISTER = 0x5005
STRING_STATUS_REGISTER = 0x5054


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

    def read_registers(self, function_code: int, start_address: int, quantity: int) -> list[int]:
        self.transaction_id = (self.transaction_id + 1) & 0xFFFF
        if self.transaction_id == 0:
            self.transaction_id = 1

        request = struct.pack(
            ">HHHBBHH",
            self.transaction_id,
            0,
            6,
            self.unit_id,
            function_code,
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
        if response_function_code == (function_code | 0x80):
            exception_code = body[1]
            raise RuntimeError(f"Modbus exception response: {exception_code}")
        if response_function_code != function_code:
            raise RuntimeError(f"unexpected Modbus function code: {response_function_code}")

        byte_count = body[1]
        register_bytes = body[2 : 2 + byte_count]
        expected_byte_count = quantity * 2
        if byte_count != expected_byte_count or len(register_bytes) != expected_byte_count:
            raise RuntimeError(f"unexpected register byte count: {byte_count}")

        return list(struct.unpack(f">{quantity}H", register_bytes))


def signed_16_from_register(register: int) -> int:
    if register & 0x8000:
        return register - 0x10000
    return register


def signed_32_from_registers(registers: list[int]) -> int:
    if len(registers) != 2:
        raise ValueError("S32 values require exactly two registers")
    value = (registers[0] << 16) | registers[1]
    if value & 0x80000000:
        value -= 0x100000000
    return value


def status_label(status: int) -> str:
    labels = {
        0: "rest",
        1: "charging",
        2: "discharging",
    }
    return labels.get(status, f"unknown ({status})")


def read_cabinet_registers(client: ModbusTcpClient, function_code: int) -> tuple[float, float, int]:
    power_register = client.read_registers(
        function_code, TOTAL_AC_OUTPUT_ACTIVE_POWER_REGISTER, 1
    )[0]
    soc_register = client.read_registers(function_code, CABINET_SOC_REGISTER, 1)[0]
    status_register = client.read_registers(function_code, CABINET_STATUS_REGISTER, 1)[0]

    soc_percent = soc_register / 10
    power_kw = signed_16_from_register(power_register) / 10
    return soc_percent, power_kw, status_register


def read_string_registers(client: ModbusTcpClient, function_code: int) -> tuple[float, float, int]:
    soc_register = client.read_registers(function_code, STRING_SOC_REGISTER, 1)[0]
    power_registers = client.read_registers(function_code, STRING_POWER_REGISTER, 2)
    status_register = client.read_registers(function_code, STRING_STATUS_REGISTER, 1)[0]

    soc_percent = soc_register / 10
    power_kw = signed_32_from_registers(power_registers) / 10
    return soc_percent, power_kw, status_register


def values_look_empty(soc_percent: float, power_kw: float, status: int) -> bool:
    return soc_percent == 0 and power_kw == 0 and status == 0


def read_battery(
    host: str,
    port: int,
    unit_id: int,
    timeout: float,
    function_code: int,
    source: str,
) -> tuple[str, float, float, int]:
    client = ModbusTcpClient(host=host, port=port, unit_id=unit_id, timeout=timeout)

    if source == "cabinet":
        soc_percent, power_kw, status = read_cabinet_registers(client, function_code)
        return source, soc_percent, power_kw, status

    if source == "string":
        soc_percent, power_kw, status = read_string_registers(client, function_code)
        return source, soc_percent, power_kw, status

    soc_percent, power_kw, status = read_cabinet_registers(client, function_code)
    if not values_look_empty(soc_percent, power_kw, status):
        return "cabinet", soc_percent, power_kw, status

    soc_percent, power_kw, status = read_string_registers(client, function_code)
    return "string", soc_percent, power_kw, status


def print_diagnostic(host: str, port: int, unit_id: int, timeout: float, function_code: int) -> bool:
    client = ModbusTcpClient(host=host, port=port, unit_id=unit_id, timeout=timeout)

    print("device: online")
    try:
        soc_percent, power_kw, status = read_cabinet_registers(client, function_code)
        print("cabinet_registers: ok")
        print(f"cabinet_soc_percent: {soc_percent:.1f}")
        print(f"cabinet_kw: {power_kw:.1f}")
        print(f"cabinet_status: {status_label(status)}")
    except Exception as exc:
        print(f"cabinet_registers: failed ({exc})")

    try:
        soc_percent, power_kw, status = read_string_registers(client, function_code)
        print("string_registers: ok")
        print(f"string_soc_percent: {soc_percent:.1f}")
        print(f"string_kw: {power_kw:.1f}")
        print(f"string_status: {status_label(status)}")
    except Exception as exc:
        print(f"string_registers: failed ({exc})")

    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read SOC and current kW from a CLOU ESS battery.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Modbus TCP host/IP")
    parser.add_argument("--port", default=DEFAULT_PORT, type=int, help="Modbus TCP port")
    parser.add_argument("--unit-id", default=DEFAULT_UNIT_ID, type=int, help="Modbus unit id")
    parser.add_argument("--timeout", default=3.0, type=float, help="Socket timeout in seconds")
    parser.add_argument(
        "--function-code",
        choices=(3, 4),
        default=FUNCTION_READ_HOLDING_REGISTERS,
        type=int,
        help="Modbus read function code. Default 3 matches the CLOU operation data query.",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "cabinet", "string"),
        default="auto",
        help="Register group to read. Auto falls back to string registers if cabinet values are all zero.",
    )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Print both cabinet and string register values to find the live register group.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.diagnostic:
        try:
            print_diagnostic(
                host=args.host,
                port=args.port,
                unit_id=args.unit_id,
                timeout=args.timeout,
                function_code=args.function_code,
            )
        except Exception as exc:
            print(f"device: offline ({exc})")
            return 1
        return 0

    try:
        source, soc_percent, power_kw, status = read_battery(
            host=args.host,
            port=args.port,
            unit_id=args.unit_id,
            timeout=args.timeout,
            function_code=args.function_code,
            source=args.source,
        )
    except Exception as exc:
        print(f"device: offline ({exc})")
        return 1

    print("device: online")
    print(f"battery_source: {source}")
    print(f"battery_soc_percent: {soc_percent:.1f}")
    print(f"battery_kw: {power_kw:.1f}")
    print(f"battery_status: {status_label(status)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
