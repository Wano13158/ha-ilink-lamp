import asyncio
import datetime as dt
import functools
from enum import StrEnum

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
)
from homeassistant.core import HassJob, HassJobType
from homeassistant.helpers import device_registry, event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .commands import ColorTempLevelUtil, ResponseStatus
from .const import (
    LOGGER,
    CONF_MAC,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_FAST,
)
from .light_bt_client import LightBtClient


class LightState(StrEnum):
    COLORTEMP = ATTR_COLOR_TEMP_KELVIN
    RGB = ATTR_RGB_COLOR
    BRIGHTNESS = ATTR_BRIGHTNESS
    POWER = "power"
    COLOR_MODE = "color_mode"


def is_white_color(rgb: tuple[int, int, int]) -> bool:
    """Determine if RGB value corresponds to white."""
    r, g, b = rgb
    if r == g == b:
        return True
    max_c = max(r, g, b)
    min_c = min(r, g, b)
    if max_c >= 220 and (max_c - min_c) <= 20:
        return True
    return False


def is_pure_white(rgb: tuple[int, int, int]) -> bool:
    """Return whether a color-picker selection is the white swatch."""
    return rgb == (255, 255, 255)


class LightCoordinator(DataUpdateCoordinator):
    _fast_poll_count = 0
    _normal_poll_interval = 60
    _fast_poll_interval = 10
    _initialized = False
    _request_status_update = True
    _unsub_update_state: event.CALLBACK_TYPE | None = None
    _concurent_update_state = 0

    def __init__(self, hass, device_id, conf):
        self.device_id = device_id
        self.device_name = conf[CONF_NAME]
        self.address = conf[CONF_MAC]
        self._normal_poll_interval = int(conf[CONF_SCAN_INTERVAL])
        self._fast_poll_interval = int(conf[CONF_SCAN_INTERVAL_FAST])

        super().__init__(
            hass,
            LOGGER,
            name="iLink Light: " + self.device_name,
            update_interval=dt.timedelta(seconds=30),
            update_method=self.async_update,
        )

        self._client = LightBtClient(hass, self.address, self._client_status_updated)

        # Initialize state in case of new integration
        self.data = {}
        self.data[LightState.COLORTEMP] = 4000
        self.data[LightState.BRIGHTNESS] = 255
        self.data[LightState.POWER] = True
        self.data[LightState.RGB] = (0xFF, 0xFF, 0xFF)
        self.data[LightState.COLOR_MODE] = ColorMode.COLOR_TEMP

    async def _client_status_updated(self, status: ResponseStatus) -> None:
        if status.temp_level is not None:
            self.data[LightState.COLORTEMP] = ColorTempLevelUtil.level_to_color_temp(
                status.temp_level
            )
            self.data[LightState.COLOR_MODE] = ColorMode.COLOR_TEMP
        elif status.rgb != (0, 0, 0):
            self.data[LightState.COLOR_MODE] = ColorMode.RGB
            self.data[LightState.RGB] = status.rgb

        self.data[LightState.BRIGHTNESS] = status.brightness
        self.data[LightState.POWER] = status.on
        if status.rgb != (0, 0, 0) and status.rgb != (255, 255, 255):
            self.data[LightState.RGB] = status.rgb

        self._request_status_update = False
        self.async_set_updated_data(self.data)

    def _set_poll_mode(self, fast: bool):
        self._fast_poll_count = 0 if fast else -1
        interval = self._fast_poll_interval if fast else self._normal_poll_interval
        self.update_interval = dt.timedelta(seconds=interval)
        self._schedule_refresh()

    def _update_poll(self):
        if self._fast_poll_count > -1:
            self._fast_poll_count += 1
            if self._fast_poll_count > 1:
                self._set_poll_mode(fast=False)

    async def _disconnect(self):
        await self._client.disconnect()

    async def async_update(self):
        if self._client.busy:
            self._set_poll_mode(fast=True)
            return self.data

        self._update_poll()

        if not self._initialized:
            await self._initialize()

        try:
            if (not self._client.waiting_status_update) or self._request_status_update:
                if await self._client.connect():
                    await self._client.request_status_update()

            await self._disconnect()
        finally:
            self._request_status_update = True

        return self.data

    async def _initialize(self):
        try:
            if self._client.service_info is not None:
                self._initialized = True
                reg = device_registry.async_get(self.hass)
                reg.async_update_device(
                    self.device_id,
                    name=self._client.service_info.name,
                    manufacturer=self._client.device_manufacturer,
                    hw_version=self._client.device_version,
                )
        except Exception as e:
            LOGGER.warning("Failed to initialize %s: %s", self.address, str(e))

    @property
    def state(self) -> dict:
        return self.data

    async def ensure_connected(self):
        if not await self._client.connect():
            raise ConnectionError("Not connected!")

    async def async_set_light_state(
        self,
        power: bool | None = None,
        brightness: int | None = None,
        rgb: tuple[int, int, int] | None = None,
        colortemp: int | None = None,
        scene: int | None = None,
    ) -> bool:
        """Unified method to apply power, brightness, RGB color, color temp, or scene atomically."""
        await self.ensure_connected()
        self._request_status_update = True

        if power is False:
            self.data[LightState.POWER] = False
            await self._client.turn_off()
            self.async_set_updated_data(self.data)
            self._set_poll_mode(fast=True)
            return True

        self.data[LightState.POWER] = True

        # Home Assistant's white swatches are reported as near-white RGB
        # values.  They must use the lamp's dedicated white LED channel;
        # treating them as RGB makes this controller show blue.
        if rgb is not None and is_white_color(rgb):
            self.data[LightState.COLOR_MODE] = ColorMode.COLOR_TEMP
            self.data[LightState.RGB] = (255, 255, 255)
            if brightness is None:
                # A colour-temperature selection contains no brightness.  Do
                # not reuse an unreliable value from the device status: one
                # tap on a white swatch must produce full brightness.
                self.data[LightState.BRIGHTNESS] = 255
            else:
                self.data[LightState.BRIGHTNESS] = brightness
            cur_br = int(self.data[LightState.BRIGHTNESS])
            cur_kelvin = self.data.get(LightState.COLORTEMP, 6000)
            level = ColorTempLevelUtil.color_temp_to_level(cur_kelvin)
            self.data[LightState.COLORTEMP] = ColorTempLevelUtil.level_to_color_temp(level)

            LOGGER.info("Setting white LED state: level=%s brightness=%s", level, cur_br)
            await self._client.set_white_temp(level)
            await self._client.set_brightness(cur_br)

        elif rgb is not None:
            self.data[LightState.COLOR_MODE] = ColorMode.RGB
            self.data[LightState.RGB] = rgb
            if brightness is not None:
                self.data[LightState.BRIGHTNESS] = brightness
            cur_br = int(self.data[LightState.BRIGHTNESS])

            LOGGER.info("Setting RGB state: color=%s, brightness=%s", rgb, cur_br)
            await self._client.set_rgb(rgb[0], rgb[1], rgb[2], cur_br)

        elif colortemp is not None:
            self.data[LightState.COLOR_MODE] = ColorMode.COLOR_TEMP
            kelvin = int(colortemp)
            level = ColorTempLevelUtil.color_temp_to_level(kelvin)
            self.data[LightState.COLORTEMP] = ColorTempLevelUtil.level_to_color_temp(level)
            if brightness is None:
                # HA's colour-temperature swatches carry only a temperature.
                # Select bright white rather than retaining a stale dim level.
                self.data[LightState.BRIGHTNESS] = 255
            else:
                self.data[LightState.BRIGHTNESS] = brightness
            cur_br = int(self.data[LightState.BRIGHTNESS])

            LOGGER.info("Setting white temp state: level=%s brightness=%s", level, cur_br)
            await self._client.set_white_temp(level)
            await self._client.set_brightness(cur_br)

        elif scene is not None:
            self.data[LightState.COLOR_MODE] = ColorMode.RGB
            await self._client.set_scene(scene)

        elif brightness is not None:
            self.data[LightState.BRIGHTNESS] = brightness
            cur_br = int(brightness)
            if self.data.get(LightState.COLOR_MODE) == ColorMode.RGB:
                cur_rgb = self.data.get(LightState.RGB, (0, 0, 255))
                LOGGER.info("Adjusting RGB brightness: %s (color: %s)", cur_br, cur_rgb)
                await self._client.set_rgb(cur_rgb[0], cur_rgb[1], cur_rgb[2], cur_br)
            else:
                LOGGER.info("Adjusting White brightness: %s", cur_br)
                await self._client.set_brightness(cur_br)

        else:
            if self.data.get(LightState.COLOR_MODE) == ColorMode.RGB:
                cur_rgb = self.data.get(LightState.RGB, (0, 0, 255))
                cur_br = int(self.data.get(LightState.BRIGHTNESS, 255))
                await self._client.set_rgb(cur_rgb[0], cur_rgb[1], cur_rgb[2], cur_br)
            else:
                cur_br = int(self.data.get(LightState.BRIGHTNESS, 255))
                cur_kelvin = self.data.get(LightState.COLORTEMP, 6000)
                level = ColorTempLevelUtil.color_temp_to_level(cur_kelvin)
                await self._client.set_white_temp(level)
                await self._client.set_brightness(cur_br)

        self.async_set_updated_data(self.data)
        self._set_poll_mode(fast=True)
        return True

    async def async_update_state(self, key: LightState, value) -> bool:
        """Legacy helper for individual state update."""
        match key:
            case LightState.POWER:
                return await self.async_set_light_state(power=bool(value))
            case LightState.BRIGHTNESS:
                return await self.async_set_light_state(brightness=int(value))
            case LightState.COLORTEMP:
                return await self.async_set_light_state(colortemp=int(value))
            case LightState.RGB:
                return await self.async_set_light_state(rgb=value)
            case "scene":
                return await self.async_set_light_state(scene=int(value))
        return False

    async def async_shutdown(self) -> None:
        await self._client.disconnect(force=True)
        await super().async_shutdown()
