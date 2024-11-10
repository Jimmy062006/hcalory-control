#!/usr/bin/env python3
import argparse
import asyncio
import dataclasses
import enum
import json
from typing import Any

import bleak
import bleak_retry_connector
import datastruct


class ListableEnum(enum.Enum):
    @classmethod
    def list(cls) -> list[str]:
        return list(cls.__members__.keys())


command_header = bytes.fromhex("000200010001000e040000090000000000000000")


class Command(bytes, ListableEnum):
    start_heat = command_header + bytes.fromhex("010e")
    stop_heat = command_header + bytes.fromhex("020f")
    up = command_header + bytes.fromhex("0310")
    down = command_header + bytes.fromhex("0411")
    gear = command_header + bytes.fromhex("0714")
    thermostat = command_header + bytes.fromhex("0613")
    # It's called pump_data because it's like you are a detective. You're
    # pumping someone for information that they _should_ just be telling you
    # but aren't.
    pump_data = command_header + bytes.fromhex("000d")


class HeaterState(int, ListableEnum):
    off = 0
    cooldown = 65
    igniting = 128
    running = 133
    idle = 135  # THIS IS A WILD GUESS. I have absolutely no clue what this is.


class HeaterMode(int, ListableEnum):
    off = 0
    thermostat = 1
    gear = 2


@dataclasses.dataclass
class HeaterResponse(datastruct.DataStruct):
    _header: bytes = datastruct.fields.field("20s")
    heater_state: HeaterState = datastruct.fields.field("B")
    heater_mode: HeaterMode = datastruct.fields.field("B")
    heater_setting: int = datastruct.fields.field("B")
    _mystery: int = datastruct.fields.field("B")
    _1: ... = datastruct.fields.padding(1)  # type: ignore
    _voltage: int = datastruct.fields.field("B")
    _2: ... = datastruct.fields.padding(1)  # type: ignore
    _body_temperature: bytes = datastruct.fields.field("2s")
    _3: ... = datastruct.fields.padding(1)  # type: ignore
    _ambient_temperature: bytes = datastruct.fields.field("2s")
    _end_junk: bytes = datastruct.fields.field("7s")

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
            "heater_mode": self.heater_mode.name,
            "heater_setting": self.heater_setting,
            "voltage": self.voltage,
            "body_temperature": self.body_temperature,
            "ambient_temperature": self.ambient_temperature,
        }


class HCaloryHeater:
    write_characteristic_id = "0000fff2-0000-1000-8000-00805f9b34fb"
    read_characteristic_id = "0000fff1-0000-1000-8000-00805f9b34fb"
    command_header = bytes.fromhex("000200010001000e040000090000000000000000")

    def __init__(self):
        self._data_pump_queue: asyncio.Queue[bytearray] = asyncio.Queue()
        self._heater_state: HeaterResponse | None = None
        self.heater_response: HeaterResponse | None = None
        self.bleak_client: bleak.BleakClient | None = None
        self._device: bleak.BLEDevice | None = None
        self._write_characteristic: bleak.BleakGATTCharacteristic | None = None
        self._read_characteristic: bleak.BleakGATTCharacteristic | None = None

    @property
    def read_characteristic(self) -> bleak.BleakGATTCharacteristic:
        if self._read_characteristic is None:
            assert self.bleak_client is not None
            self._read_characteristic = self.bleak_client.services.get_characteristic(
                self.read_characteristic_id
            )
        assert self._read_characteristic is not None
        return self._read_characteristic

    @property
    def write_characteristic(self) -> bleak.BleakGATTCharacteristic:
        if self._write_characteristic is None:
            assert self.bleak_client is not None
            self._write_characteristic = self.bleak_client.services.get_characteristic(
                self.write_characteristic_id
            )
        assert self._write_characteristic is not None
        return self._write_characteristic

    def handle_disconnect(self, _: bleak.BleakClient) -> None:
        assert self._device is not None
        asyncio.get_event_loop().create_task(self.connect(self._device))

    async def data_pump_handler(
        self, _: bleak.BleakGATTCharacteristic, data: bytearray
    ) -> None:
        await self._data_pump_queue.put(data)

    async def get_data(self) -> HeaterResponse:
        await self.send_command(Command.pump_data)
        self.heater_response = HeaterResponse.unpack(await self._data_pump_queue.get())
        assert self.heater_response is not None
        return self.heater_response

    async def send_command(self, command: Command):
        assert self.bleak_client is not None
        await self.bleak_client.write_gatt_char(self.write_characteristic, command)

    async def connect(self, device: bleak.BLEDevice) -> None:
        self._device = device
        self.bleak_client = await bleak_retry_connector.establish_connection(
            bleak.BleakClient,
            device,
            device.address,
            self.handle_disconnect,
            use_services_cache=True,
            max_attempts=20,
        )
        await self.bleak_client.start_notify(
            self.read_characteristic, self.data_pump_handler
        )


async def run_command(command: Command, address: str) -> None:
    device = await bleak.BleakScanner.find_device_by_address(address, timeout=30.0)
    assert device is not None
    heater = HCaloryHeater()
    await heater.connect(device)
    pre_command_data = await heater.get_data()
    if command == Command.pump_data:
        print(json.dumps(pre_command_data.asdict(), sort_keys=True, indent=4))
        return
    await heater.send_command(command)
    # It unfortunately takes a sec for the heater to actually respond. There's no way to confirm the change without
    # just sleeping.
    await asyncio.sleep(1)
    post_command_data = await heater.get_data()
    print(
        f"Before command:\n{json.dumps(pre_command_data.asdict(), sort_keys=True, indent=4)}"
    )
    print(
        f"After command:\n{json.dumps(post_command_data.asdict(), sort_keys=True, indent=4)}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", type=str, choices=Command.list())
    parser.add_argument(
        "--address", type=str, help="Bluetooth MAC address of heater", required=True
    )
    args = parser.parse_args()
    command = Command[args.command]
    address: str = args.address
    # Listen here, Pycharm. This is an _enum_. It is never instantiated.
    # The type will _always_ be Command. Stop complaining about this!
    # noinspection PyTypeChecker
    asyncio.run(run_command(command, address))


if __name__ == "__main__":
    main()
