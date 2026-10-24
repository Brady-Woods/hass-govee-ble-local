"""The colour conversions Home Assistant's light service applies, so the plan can predict them.

Ported from ``homeassistant/util/color.py`` (Home Assistant 2026.10, Apache-2.0), with the
calls ``homeassistant/components/light/helper.py`` makes for a parameter the light doesn't
support: a colour temperature on a light without COLOR_TEMP becomes an HS colour, then RGB
(``process_turn_on_params``); an RGB colour on a light without RGB but with COLOR_TEMP
becomes a Kelvin through CIE xy (McCamy's approximation). The harness can't import Home
Assistant (it runs with the host's Python), so these are copies, kept to the same rounding.
"""

from __future__ import annotations

import colorsys
import math

RGB = tuple[int, int, int]

_MIN_KELVIN, _MAX_KELVIN = 1000, 40000
_HELLAND_KNEE = 66
_HELLAND_NO_BLUE = 19
_SRGB_LINEAR_KNEE = 0.04045


def _clamp(value: float, low: float = 0, high: float = 255) -> float:
    return min(max(value, low), high)


def _red(temperature: float) -> float:
    if temperature <= _HELLAND_KNEE:
        return 255
    return _clamp(329.698727446 * math.pow(temperature - 60, -0.1332047592))


def _green(temperature: float) -> float:
    if temperature <= _HELLAND_KNEE:
        return _clamp(99.4708025861 * math.log(temperature) - 161.1195681661)
    return _clamp(288.1221695283 * math.pow(temperature - 60, -0.0755148492))


def _blue(temperature: float) -> float:
    if temperature >= _HELLAND_KNEE:
        return 255
    if temperature <= _HELLAND_NO_BLUE:
        return 0
    return _clamp(138.5177312231 * math.log(temperature - 10) - 305.0447927307)


def temperature_to_rgb(kelvin: float) -> tuple[float, float, float]:
    """``color_temperature_to_rgb``: T. Helland's approximation."""
    temperature = min(max(kelvin, _MIN_KELVIN), _MAX_KELVIN) / 100.0
    return _red(temperature), _green(temperature), _blue(temperature)


def rgb_to_hs(red: float, green: float, blue: float) -> tuple[float, float]:
    """``color_RGB_to_hs``."""
    hue, sat, _ = colorsys.rgb_to_hsv(red / 255.0, green / 255.0, blue / 255.0)
    return round(hue * 360, 3), round(sat * 100, 3)


def hs_to_rgb(hue: float, sat: float) -> RGB:
    """``color_hs_to_RGB`` (full value)."""
    red, green, blue = colorsys.hsv_to_rgb(hue / 360, sat / 100, 1.0)
    return round(red * 255), round(green * 255), round(blue * 255)


def kelvin_as_rgb(kelvin: int) -> RGB:
    """Return the RGB Home Assistant sends an RGB light (no COLOR_TEMP) asked for ``kelvin``."""
    return hs_to_rgb(*rgb_to_hs(*temperature_to_rgb(kelvin)))


def _linear(channel: int) -> float:
    value = channel / 255
    if value > _SRGB_LINEAR_KNEE:
        return math.pow((value + 0.055) / 1.055, 2.4)
    return value / 12.92


def rgb_to_xy(red: int, green: int, blue: int) -> tuple[float, float]:
    """``color_RGB_to_xy`` (no gamut): Wide RGB D65."""
    if red + green + blue == 0:
        return 0.0, 0.0
    r, g, b = _linear(red), _linear(green), _linear(blue)
    x_ = r * 0.664511 + g * 0.154324 + b * 0.162028
    y_ = r * 0.283881 + g * 0.668433 + b * 0.047685
    z_ = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x_ + y_ + z_
    return round(x_ / total, 3), round(y_ / total, 3)


def xy_to_temperature(x: float, y: float) -> int:
    """``color_xy_to_temperature``: McCamy's approximation."""
    n = (x - 0.3320) / (0.1858 - y)
    return int(437 * n**3 + 3601 * n**2 + 6861 * n + 5517)


def rgb_as_kelvin(rgb: RGB) -> int:
    """Return the Kelvin HA sends a colour-temperature-only light asked for ``rgb``."""
    return xy_to_temperature(*rgb_to_xy(*rgb))
