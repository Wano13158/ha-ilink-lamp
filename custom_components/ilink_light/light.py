from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityDescription,
    LightEntityFeature,
)
from homeassistant.const import CONF_DEVICES

from .commands import ColorTempLevelUtil, Scenes
from .const import CONF_NAME, DOMAIN, LOGGER
from .coordinator import LightCoordinator, LightState
from .entity import iLinkLightBaseEntity

light_description = LightEntityDescription(
    key="light",
    name="Light",
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    ha_entities = []

    for device_id in config_entry.data[CONF_DEVICES]:
        LOGGER.debug("Starting iLink lights: %s", config_entry.data[CONF_DEVICES])
        LOGGER.debug(
            "Starting iLink lights: %s",
            config_entry.data[CONF_DEVICES][device_id][CONF_NAME],
        )

        coordinator = hass.data[DOMAIN][CONF_DEVICES][device_id]
        ha_entities.append(iLinkLightEntity(coordinator, light_description))

    async_add_entities(ha_entities, True)


class iLinkLightEntity(iLinkLightBaseEntity, LightEntity):
    min_color_temp_kelvin = 3000
    max_color_temp_kelvin = 6000

    _attr_supported_color_modes = {
        ColorMode.COLOR_TEMP,
        ColorMode.RGB,
    }
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect = None

    def __init__(
        self, coordinator: LightCoordinator, description: LightEntityDescription
    ) -> None:
        super().__init__(coordinator, description)
        self._attr_effect_list = ["100%", "Sleep"] + Scenes.all()

    @property
    def brightness(self) -> int | None:
        return self.coordinator.state.get(LightState.BRIGHTNESS)

    @property
    def color_temp_kelvin(self) -> int | None:
        return self.coordinator.state.get(LightState.COLORTEMP)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self.coordinator.state.get(LightState.RGB)

    @property
    def color_mode(self) -> ColorMode:
        return self.coordinator.state.get(LightState.COLOR_MODE, ColorMode.COLOR_TEMP)

    @property
    def effect(self) -> str | None:
        return self._attr_effect

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.state.get(LightState.POWER, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light or change color/brightness."""
        rgb = kwargs.get(ATTR_RGB_COLOR)
        colortemp = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        effect = kwargs.get(ATTR_EFFECT)
        scene = None

        if effect:
            self._attr_effect = effect
            if effect == "100%":
                colortemp = ColorTempLevelUtil.level_to_color_temp(3)
                brightness = 255
            elif effect == "Sleep":
                colortemp = ColorTempLevelUtil.level_to_color_temp(5)
                brightness = 4
            else:
                scene = Scenes.name_to_id(effect)
        elif rgb or colortemp:
            self._attr_effect = None

        # Optimistically set power state in local dict
        self.coordinator.state[LightState.POWER] = True
        self.async_write_ha_state()

        await self.coordinator.async_set_light_state(
            power=True,
            brightness=brightness,
            rgb=rgb,
            colortemp=colortemp,
            scene=scene,
        )
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        self._attr_effect = None
        self.coordinator.state[LightState.POWER] = False
        self.async_write_ha_state()

        await self.coordinator.async_set_light_state(power=False)
        self.async_write_ha_state()
