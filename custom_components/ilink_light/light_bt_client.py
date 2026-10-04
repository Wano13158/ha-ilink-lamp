import asyncio
import sys
from typing import Awaitable, Callable

if sys.version_info >= (3, 11):
    from asyncio import timeout as async_timeout
else:
    import async_timeout

from bleak import BleakClient, BleakGATTCharacteristic, BLEDevice
from bleak.exc import BleakError
from home_assistant_bluetooth import BluetoothServiceInfoBleak

from homeassistant.components import bluetooth

from .commands import (
    CHARACTERISTIC_REQUEST_STATUS,
    CHARACTERISTIC_SEND_CMD,
    CHARACTERISTIC_SEND_CMD_NO_RESP,
    Commands,
    Response,
    ResponseStatus,
)
from .const import LOGGER


class LightBtClient:
    service_info: BluetoothServiceInfoBleak | None = None
    device_manufacturer: str | None = None
    device_version: str | None = None
    _status: ResponseStatus | None = None
    _callback: Callable[[ResponseStatus], Awaitable[None]] | None = None
    _current_connect = None
    waiting_status_update = False
    _disconnect_next = False
    _busy = False
    _connecting = False
    _ble_device: BLEDevice | None = None

    def __init__(
        self,
        hass,
        address: str,
        callback: Callable[[ResponseStatus], Awaitable[None]] | None = None,
    ):
        self._hass = hass
        self._bt_client: BleakClient | None = None
        self._address = address
        self._send_command_err_count = 0
        self._callback = callback

    @property
    def busy(self) -> bool:
        return self._connecting or (self._busy and self.is_connected())

    async def _notification_handler(
        self, characteristic: BleakGATTCharacteristic, data: bytearray
    ):
        LOGGER.debug(
            "_notification_handler received %s: %s: %s - %r",
            self._address,
            characteristic.description,
            data.hex(),
            data,
        )

        if Response.is_status(data):
            status = Response.parse_status(data)
            LOGGER.info("status received %s: %s", self._address, vars(status))
            self._status = status
            if self._callback:
                await self._callback(status)
            self.waiting_status_update = False

        await self.disconnect(only_if_needed=True)

    async def _initialize(self) -> None:
        try:
            self._busy = False
            self.waiting_status_update = False
            self._disconnect_next = False
            self.service_info = bluetooth.async_last_service_info(
                self._hass, self._address, connectable=True
            )

            if self.service_info and self.service_info.manufacturer_data:
                LOGGER.debug(
                    "_initialize %s service_info.adv: %s",
                    self._address,
                    self.service_info.advertisement,
                )
                md = self.service_info.manufacturer_data
                if value := md.get(5101, None):
                    self.device_version = f"{value[0]}.{value[1]}.{value[2]}.{value[3]}"
                if value := md.get(1494, None):
                    try:
                        self.device_manufacturer = value.decode("ascii")
                    except Exception:
                        self.device_manufacturer = None

            if self._bt_client:
                try:
                    await self._bt_client.start_notify(
                        CHARACTERISTIC_REQUEST_STATUS, self._notification_handler
                    )
                except Exception as notify_err:
                    LOGGER.debug(
                        "Could not start notify on %s: %s", self._address, notify_err
                    )

            if self.status is None:
                await self.request_status_update()

            LOGGER.debug("initialized %s", self._address)
        except Exception as e:
            LOGGER.warning("initialize error: %s", str(e), exc_info=e)

    async def connect(self, retries=3) -> bool:
        try:
            if self._current_connect is None:
                self._current_connect = self._connect(retries)
            result = await self._current_connect
            return result
        finally:
            self._current_connect = None

    async def _connect(self, retries=3) -> bool:
        if self.is_connected():
            return True
        if self._connecting:
            return False

        tries = 0
        self._connecting = True

        LOGGER.debug("Connecting to %s", self._address)
        while tries < retries:
            tries += 1

            try:
                if self._bt_client is None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self._hass, self._address.upper()
                    )
                    if ble_device:
                        self._ble_device = ble_device
                    if not self._ble_device:
                        raise BleakError(
                            f"A device with address {self._address} could not be found."
                        )
                    self._bt_client = BleakClient(self._ble_device)
                ret = await self._bt_client.connect()
                if ret:
                    LOGGER.debug("Connected to %s", self._address)
                    await self._initialize()
                    break
            except Exception as e:
                if tries == retries:
                    LOGGER.info("Not able to connect to %s! %s", self._address, str(e))
                else:
                    LOGGER.debug("Retrying %s", self._address)
                    await asyncio.sleep(1)
        self._connecting = False
        return self.is_connected()

    async def disconnect(
        self, force: bool = False, only_if_needed: bool = False
    ) -> None:
        if not force:
            if self.busy or self.waiting_status_update:
                self._disconnect_next = True
                return
            elif only_if_needed and not self._disconnect_next:
                return

        self.waiting_status_update = False
        self._busy = False
        self._disconnect_next = False

        if self.is_connected():
            try:
                LOGGER.debug("disconnecting %s", self._address)
                await self._bt_client.disconnect()
            except Exception as e:
                LOGGER.warning("Error disconnecting %s! %s", self._address, str(e))
            if self.status is None:
                self._bt_client = None

    @property
    def status(self) -> ResponseStatus | None:
        return self._status

    def is_connected(self) -> bool:
        return self._bt_client is not None and self._bt_client.is_connected

    async def _write_uuid(self, uuid: str, val: bytes, response: bool = True) -> None:
        if self._busy:
            raise RuntimeError("device busy")
        try:
            self._busy = True
            await self._bt_client.write_gatt_char(
                char_specifier=uuid, data=val, response=response
            )
        finally:
            self._busy = False

    async def _send_payload(self, data: bytes) -> None:
        LOGGER.debug("send payload %s: %s", self._address, data.hex())
        if not self.is_connected():
            if not await self.connect():
                LOGGER.warning("Cannot send payload - not connected to %s", self._address)
                return

        # Determine target characteristic and whether write-without-response is supported
        target_char = CHARACTERISTIC_SEND_CMD
        use_response = True

        if self._bt_client and self._bt_client.services:
            char_44 = self._bt_client.services.get_characteristic(
                CHARACTERISTIC_SEND_CMD_NO_RESP
            )
            if char_44:
                target_char = CHARACTERISTIC_SEND_CMD_NO_RESP
                use_response = False
            else:
                char_40 = self._bt_client.services.get_characteristic(
                    CHARACTERISTIC_SEND_CMD
                )
                if char_40 and "write-without-response" in char_40.properties:
                    use_response = False

        try:
            async with async_timeout(2):
                await self._write_uuid(target_char, data, response=use_response)
            self._send_command_err_count = 0
        except Exception as e:
            # Fallback to standard write characteristic with response=True
            if target_char != CHARACTERISTIC_SEND_CMD:
                try:
                    async with async_timeout(2):
                        await self._write_uuid(
                            CHARACTERISTIC_SEND_CMD, data, response=True
                        )
                    self._send_command_err_count = 0
                    return
                except Exception as inner_e:
                    e = inner_e

            self._send_command_err_count += 1
            if self._send_command_err_count > 10:
                LOGGER.info(
                    "%s errors occurred in send payload %s! Last: %s",
                    self._send_command_err_count,
                    self._address,
                    str(e),
                )
                self._send_command_err_count = 0

    async def _send_command(self, command: str) -> None:
        await self._send_payload(bytes.fromhex(command))

    async def request_status_update(self) -> None:
        self.waiting_status_update = True
        LOGGER.debug("request_status_update %s", self._address)
        await self._send_command(Commands.status())

    async def set_brightness(self, value: int) -> None:
        if value < 0 or value > 0xFF:
            raise ValueError("Brightness must be between 0 and 255")

        LOGGER.debug("set_brightness: %s", value)
        await self._send_command(Commands.brightness(value))

    async def set_white_temp(self, value: int) -> None:
        if value < 1 or value > 5:
            raise ValueError("White temperature must be between 1 and 5")

        LOGGER.debug("set_white_temp: %s", value)
        await self._send_command(Commands.white_temp(value))

    async def set_rgb(self, r: int, g: int, b: int, brightness: int = 255) -> None:
        if r < 0 or r > 0xFF or g < 0 or g > 0xFF or b < 0 or b > 0xFF:
            raise ValueError("RGB values must be between 0 and 255")
        if brightness < 0 or brightness > 0xFF:
            raise ValueError("Brightness must be between 0 and 255")

        LOGGER.debug("set_rgb: %s %s %s brightness: %s", r, g, b, brightness)
        # Scale RGB proportionally by brightness
        if brightness == 0:
            r_val, g_val, b_val = 0, 0, 0
        else:
            r_val = max(1 if r > 0 else 0, round(r * brightness / 255))
            g_val = max(1 if g > 0 else 0, round(g * brightness / 255))
            b_val = max(1 if b > 0 else 0, round(b * brightness / 255))

        std_rgb_cmd = bytes.fromhex(Commands.rgb(r_val, g_val, b_val))
        await self._send_payload(std_rgb_cmd)

    async def set_scene(self, value: int) -> None:
        if value < 1 or value > 93:
            raise ValueError("Scene must be between 1 and 93")

        LOGGER.debug("set_scene: %s", value)
        await self._send_command(Commands.scene(value))

    async def turn_on(self) -> None:
        LOGGER.debug("turn_on")
        await self._send_command(Commands.on())

    async def turn_off(self) -> None:
        LOGGER.debug("turn_off")
        await self._send_command(Commands.off())
