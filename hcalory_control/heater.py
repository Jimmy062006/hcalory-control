#!/usr/bin/env python3
import argparse
import asyncio
import dataclasses
import enum
import json
import logging
from typing import Any, Optional

import bleak
import bleak_retry_connector
import datastruct
from bleak import BleakError, BleakGATTCharacteristic, BleakClient, BLEDevice

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

class ListableEnum(enum.Enum):
    @classmethod
    def list(cls) -> list[str]:
        return list(cls.__members__.keys())

command_header = bytes.fromhex("000200010001000e040000090000000000000000")

class Command(bytes, ListableEnum):
    stop_heat = command_header + bytes.fromhex("010e")
    start_heat = command_header + bytes.fromhex("020f")
    up = command_header + bytes.fromhex("0310")
    down = command_header + bytes.fromhex("0411")
    gear = command_header + bytes.fromhex("0714")
    thermostat = command_header + bytes.fromhex("0613")
    pump_data = command_header + bytes.fromhex("000d")

class HeaterState(int, ListableEnum):
    off = 0
    cooldown = 65
    cooldown_starting = 67
    cooldown_received = 69
    ignition_received = 128
    ignition_starting = 129
    igniting = 131
    running = 133
    heating = 135
    error = 255

@dataclasses.dataclass
class HeaterResponse(datastruct.DataStruct):
    _header: bytes = datastruct.fields.field("20s")
    heater_state: HeaterState = datastruct.fields.field("B")
    heater_mode: int = datastruct.fields.field("B")
    heater_setting: int = datastruct.fields.field("B")
    _voltage: int = datastruct.fields.field("B")
    _body_temperature: bytes = datastruct.fields.field("2s")
    _ambient_temperature: bytes = datastruct.fields.field("2s")

    @property
    def voltage(self) -> int:
        return self._voltage // 10

    @property
    def body_temperature(self) -> int:
        return int(self._body_temperature.hex(), 16) // 10

    @property
    def ambient_temperature(self) -> int:
        return int(self._ambient_temperature.hex(), 16) // 10

    def asdict(self) -> dict[str, Any]:
        return {
            "heater_state": self.heater_state.name,
            "heater_mode": self.heater_mode,
            "heater_setting": self.heater_setting,
            "voltage": self.voltage,
            "body_temperature": self.body_temperature,
            "ambient_temperature": self.ambient_temperature,
        }

class HCaloryHeater:
    write_characteristic_id = "0000fff2-0000-1000-8000-00805f9b34fb"
    read_characteristic_id = "0000fff1-0000-1000-8000-00805f9b34fb"

    def __init__(
        self, device: BLEDevice, bluetooth_timeout: float = 30.0, max_retries: int = 20
    ):
        self.device = device
        self.bluetooth_timeout = bluetooth_timeout
        self.max_retries = max_retries
        self.bleak_client: Optional[BleakClient] = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()

    async def connect(self):
        try:
            self.bleak_client = await bleak_retry_connector.establish_connection(
                BleakClient, self.device, self.device.address, self.handle_disconnect,
                use_services_cache=True, max_attempts=self.max_retries, timeout=self.bluetooth_timeout
            )
            logger.info("Connected to heater: %s", self.device.address)
        except (asyncio.TimeoutError, BleakError) as e:
            logger.error("Failed to connect to heater %s: %s", self.device.address, e)
            raise

    async def disconnect(self):
        if self.bleak_client:
            await self.bleak_client.disconnect()
            logger.info("Disconnected from heater: %s", self.device.address)

    async def send_command(self, command: Command):
        if not self.bleak_client or not self.bleak_client.is_connected:
            await self.connect()
        try:
            await self.bleak_client.write_gatt_char(self.write_characteristic_id, command)
            logger.info("Sent command %s to heater %s", command.name, self.device.address)
        except BleakError as e:
            logger.error("Failed to send command %s: %s", command.name, e)
            raise

    async def get_data(self) -> HeaterResponse:
        await self.send_command(Command.pump_data)
        if not self.bleak_client:
            raise RuntimeError("Not connected to heater")
        data = await self.bleak_client.read_gatt_char(self.read_characteristic_id)
        response = HeaterResponse.unpack(data)
        logger.info("Received data: %s", response.asdict())
        return response

    def handle_disconnect(self, _: BleakClient):
        logger.warning("Unexpected disconnect from heater %s", self.device.address)

async def run_command(command: Command, address: str):
    device = await bleak.BleakScanner.find_device_by_address(address, timeout=30.0)
    if not device:
        logger.error("Device not found at address: %s", address)
        return
    
    async with HCaloryHeater(device) as heater:
        pre_data = await heater.get_data()
        if command == Command.pump_data:
            print(json.dumps(pre_data.asdict(), indent=4))
            return
        await heater.send_command(command)
        await asyncio.sleep(1)
        post_data = await heater.get_data()
        print("Before command:", json.dumps(pre_data.asdict(), indent=4))
        print("After command:", json.dumps(post_data.asdict(), indent=4))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", type=str, choices=Command.list())
    parser.add_argument("--address", type=str, required=True, help="Bluetooth MAC address of heater")
    args = parser.parse_args()
    
    command = Command[args.command]
    asyncio.run(run_command(command, args.address))

if __name__ == "__main__":
    main()
