#!/usr/bin/env python3
import argparse
import asyncio
import dataclasses
import enum
import json
import logging
from typing import Any

import bleak
import bleak_retry_connector
import datastruct
from bleak import BleakError

logger = logging.getLogger(__name__)


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
    # It's called pump_data because it's like you are a detective. You're
    # pumping someone for information that they _should_ just be telling you
    # but aren't.
    pump_data = command_header + bytes.fromhex("000d")


class HeaterState(int, ListableEnum):
    """
    The current state of the heater.
    This heater has such a weird state machine. Here are the state transitions I've observed:
    off -> ignition_received -> ignition_starting -> igniting -> heating -> running
    running -> cooldown_received -> cooldown_starting -> cooldown -> off
    """

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


class HeaterMode(int, ListableEnum):
    off = 0
    thermostat = 1
    gear = 2
    ignition_failed = 8


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

    @property
    def cooldown(self) -> bool:
        """
        Returns whether or not the heater is in its uninterruptible post-stop cooldown stage
        """
        # maybe a bit weird to use a case statement here, but it's more concise than doing an if any().
        match self.heater_state:
            case (
                HeaterState.cooldown_received
                | HeaterState.cooldown_starting
                | HeaterState.cooldown
            ):
                return True
        return False

    @property
    def preheating(self) -> bool:
        """
        Returns whether or not the heater is in its uninterruptible pre-start ignition and heating stage
        """
        match self.heater_state:
            case (
                HeaterState.ignition_received
                | HeaterState.ignition_starting
                | HeaterState.igniting
                | HeaterState.heating
            ):
                return True
        return False

    @property
    def running(self) -> bool:
        match self.heater_state:
            case (
                HeaterState.off
                | HeaterState.cooldown
                | HeaterState.cooldown_received
                | HeaterState.cooldown_starting
                | HeaterState.error
            ):
                return False
            case (
                HeaterState.ignition_received
                | HeaterState.ignition_starting
                | HeaterState.igniting
                | HeaterState.running
                | HeaterState.heating
            ):
                return True
        return False

    def asdict(self) -> dict[str, Any]:
        return {
            "heater_state": self.heater_state.name,
            "heater_mode": self.heater_mode.name,
            "heater_setting": self.heater_setting,
            "voltage": self.voltage,
            "body_temperature": self.body_temperature,
            "ambient_temperature": self.ambient_temperature,
            "running": self.running,
            "cooldown": self.cooldown,
            "preheating": self.preheating,
        }


class HCaloryHeater:
    write_characteristic_id = "0000fff2-0000-1000-8000-00805f9b34fb"
    read_characteristic_id = "0000fff1-0000-1000-8000-00805f9b34fb"

    def __init__(
        self,
        device: bleak.BLEDevice,
        bluetooth_timeout: float = 30.0,
        max_bluetooth_retry_attempts: int = 20,
    ):
        self.device: bleak.BLEDevice = device
        self.bluetooth_timeout: float = bluetooth_timeout
        self.max_bluetooth_retry_attempts = max_bluetooth_retry_attempts
        self._data_pump_queue: asyncio.Queue[bytearray] = asyncio.Queue()
        self._heater_state: HeaterResponse | None = None
        self.heater_response: HeaterResponse | None = None
        self.bleak_client: bleak.BleakClient | None = None
        self._write_characteristic: bleak.BleakGATTCharacteristic | None = None
        self._read_characteristic: bleak.BleakGATTCharacteristic | None = None
        self._intentional_disconnect: bool = False
        self._reconnect_event: asyncio.Event = asyncio.Event()
        self._reconnect_event.clear()
        self._connect_lock = asyncio.Lock()
        self._command_lock = asyncio.Lock()

    async def start_heat(self):
        await self.send_command(Command.start_heat)

    async def stop_heat(self):
        await self.send_command(Command.stop_heat)

    async def change_setting_up(self):
        await self.send_command(Command.up)

    async def change_setting_down(self):
        await self.send_command(Command.down)

    async def set_thermostat_mode(self):
        await self.send_command(Command.thermostat)

    async def set_gear_mode(self):
        await self.send_command(Command.gear)

    async def _ensure_connection(self, connection_reason: str = "") -> None:
        if connection_reason:
            connection_reason = f" Reason: {connection_reason}"
        if self._connect_lock.locked():
            logger.debug(
                "Connection to heater %s already ongoing. You'll have to wait!%s",
                self.device.address,
                connection_reason,
            )
        if self.bleak_client is not None and self.bleak_client.is_connected:
            logger.debug(
                "Already connected to %s, connection is ensured.%s",
                self.device.address,
                connection_reason,
            )
            return
        async with self._connect_lock:
            if self.bleak_client is not None and self.bleak_client.is_connected:
                logger.debug(
                    "(Locked) Already connected to %s, connection is ensured.%s",
                    self.device.address,
                    connection_reason,
                )
                return
            try:
                client = await bleak_retry_connector.establish_connection(
                    bleak.BleakClient,
                    self.device,
                    self.device.address,
                    self.handle_disconnect,
                    use_services_cache=True,
                    max_attempts=self.max_bluetooth_retry_attempts,
                    timeout=self.bluetooth_timeout,
                )
                self._intentional_disconnect = False
                self._reconnect_event.set()
                self.bleak_client = client
            except (asyncio.TimeoutError, BleakError):
                logger.exception(
                    "(Locked) Failed to connect to heater %s. This will be retried.%s",
                    self.device.address,
                    connection_reason,
                )
                raise
            await self.bleak_client.start_notify(
                self.read_characteristic, self.data_pump_handler
            )

    @property
    def read_characteristic(self) -> bleak.BleakGATTCharacteristic:
        if self._read_characteristic is None:
            assert self.bleak_client is not None
            read_characteristic = self.bleak_client.services.get_characteristic(
                self.read_characteristic_id
            )
            assert read_characteristic is not None
            self._read_characteristic = read_characteristic
        return self._read_characteristic

    @property
    def write_characteristic(self) -> bleak.BleakGATTCharacteristic:
        if self._write_characteristic is None:
            assert self.bleak_client is not None
            write_characteristic = self.bleak_client.services.get_characteristic(
                self.write_characteristic_id
            )
            assert write_characteristic is not None
            self._write_characteristic = write_characteristic
        return self._write_characteristic

    def handle_disconnect(self, _: bleak.BleakClient) -> None:
        self._reconnect_event.clear()
        if not self._intentional_disconnect:
            logger.warning(
                "Encountered unintentional disconnect from %s.", self.device.address
            )
        else:
            logger.debug(
                "Encountered intentional disconnect from %s.", self.device.address
            )

    async def data_pump_handler(
        self, _: bleak.BleakGATTCharacteristic, data: bytearray
    ) -> None:
        await self._data_pump_queue.put(data)

    async def get_data(self) -> HeaterResponse:
        await self.send_command(Command.pump_data)
        self.heater_response = HeaterResponse.unpack(await self._data_pump_queue.get())
        assert self.heater_response is not None
        return self.heater_response

    async def disconnect(self) -> None:
        self._intentional_disconnect = True
        assert self.bleak_client is not None
        await self.bleak_client.disconnect()

    async def send_command(self, command: Command):
        logger.debug(
            "Sending %s to heater %s",
            command.name,
            self.device.address,
        )
        async with self._command_lock:
            await self._ensure_connection(f"Sending command {command.name}")
            # I wish there was a way to indicate that _ensure_connection is performing this assert.
            assert self.bleak_client is not None
            await self.bleak_client.write_gatt_char(self.write_characteristic, command)
            logger.debug(
                "Sent %s to characteristic %s of heater %s",
                command.name,
                self.write_characteristic,
                self.device.address,
            )

    async def wait_for_reconnect(self, timeout: float = 30.0) -> None:
        async with asyncio.timeout(timeout):
            await self._reconnect_event.wait()


async def run_command(command: Command, address: str) -> None:
    device = await bleak.BleakScanner.find_device_by_address(address, timeout=30.0)
    assert device is not None
    heater = HCaloryHeater(device)
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
