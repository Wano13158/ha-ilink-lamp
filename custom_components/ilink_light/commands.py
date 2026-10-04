from .scenes import all_scenes

SERVICE_UUID = "0000a032-0000-1000-8000-00805f9b34fb"
"""[Characteristic] 0000a040-0000-1000-8000-00805f9b34fb: Vendor specific (write)"""
CHARACTERISTIC_SEND_CMD = "0000a040-0000-1000-8000-00805f9b34fb"
"""[Characteristic] 0000a044-0000-1000-8000-00805f9b34fb: Vendor specific (write-without-response,write)"""
CHARACTERISTIC_SEND_CMD_NO_RESP = "0000a044-0000-1000-8000-00805f9b34fb"
"""[Characteristic] 0000a042-0000-1000-8000-00805f9b34fb: Vendor specific (notify)"""
CHARACTERISTIC_REQUEST_STATUS = "0000a042-0000-1000-8000-00805f9b34fb"
"""[Characteristic] 0000a041-0000-1000-8000-00805f9b34fb: Vendor specific (read status)"""
CHARACTERISTIC_READ_STATUS = "0000a041-0000-1000-8000-00805f9b34fb"

color_temp_mappings = {
    1: 6000,  # cold white
    2: 5000,  # nature light
    3: 4000,  # sun light
    4: 3500,  # sun set
    5: 3000,  # candle light
}


class Scenes:
    @staticmethod
    def all() -> list[str]:
        return list(all_scenes.values())

    @staticmethod
    def some() -> list[str]:
        return list(all_scenes.values())[:11]

    @staticmethod
    def id_to_name(id: int) -> str | None:
        return all_scenes.get(id)

    @staticmethod
    def name_to_id(name: str) -> int | None:
        value = [i for i in all_scenes if all_scenes[i] == name]
        return value[0] if len(value) > 0 else None


class ColorTempLevelUtil:
    @staticmethod
    def color_temp_to_level(temp: int) -> int:
        if temp > 6000:
            temp = 6000
        elif temp < 3000:
            temp = 3000

        return min(
            color_temp_mappings.keys(),
            key=lambda k: abs(color_temp_mappings[k] - temp),
        )

    @staticmethod
    def level_to_color_temp(level: int) -> int:
        if level > 5:
            level = 5
        elif level < 1:
            level = 1
        return color_temp_mappings[level]


class Commands:
    _header = "55aa"
    """rgb or white mode ?"""
    _header_std = "01"
    _header_rgb = "03"
    """on/off command"""
    _cmd_switch = "0805"
    _cmd_switch_on = "01"  # f1"
    _cmd_switch_off = "00"  # f2"
    """dim white command"""
    _cmd_dim = "0801"
    _cmd_rgb = "0802"
    """white temperature commands"""
    _cmd_white_temp = "0809"
    _cmd_white_temp_white = "01ed"  # 6000K
    _cmd_white_temp_nature_light = "02ec"  # 5000K
    _cmd_white_temp_sun_light = "03eb"  # 4000K
    _cmd_white_temp_sun_set = "04ea"  # 3500K
    _cmd_white_temp_candle_light = "05e9"  # 3000K
    """status command"""
    _cmd_status = "0815"
    _cmd_status_all = "06"
    """scene command"""
    _cmd_scene = "0e20"

    @staticmethod
    def _cmd(hdr: str, data: str):
        cmd = f"{Commands._header}{hdr}{data}"
        crc = Commands._crc(cmd)
        return f"{cmd}{crc}"

    @staticmethod
    def _std(command: str, params: str):
        return Commands._cmd(Commands._header_std, f"{command}{params}")

    @staticmethod
    def _rgb(command: str, params: str):
        return Commands._cmd(Commands._header_rgb, f"{command}{params}")

    @staticmethod
    def _crc(data: str) -> str:
        all_sum = 0
        for i in range(0, len(data), 2):
            all_sum += int(data[i : i + 2], 16)
        crc = (0xFF - all_sum) & 0xFF
        return f"{crc:02x}"

    @staticmethod
    def on():
        """turn on (white light)"""
        return Commands._std(Commands._cmd_switch, Commands._cmd_switch_on)

    @staticmethod
    def status():
        """request status notification 55aa01081506dc"""
        return Commands._std(Commands._cmd_status, Commands._cmd_status_all)

    @staticmethod
    def off():
        """turn off"""
        return Commands._std(Commands._cmd_switch, Commands._cmd_switch_off)

    @staticmethod
    def brightness(value: int):
        """brightness command for white mode from 0 to 255"""
        if value > 0xFF:
            value = 0xFF
        elif value < 0:
            value = 0

        return Commands._std(Commands._cmd_dim, f"{value:02x}")

    @staticmethod
    def rgb(r: int, g: int, b: int):
        """rgb value (55aa framing)"""
        return Commands._rgb(Commands._cmd_rgb, f"{r:02x}{g:02x}{b:02x}")

    @staticmethod
    def rgb_btsnoop(r: int, g: int, b: int, brightness: int = 255) -> bytes:
        """RGB packet from official app btsnoop logs (0x55, r, g, b, brightness, 0xFF, 0xF0)"""
        r_val = max(0, min(255, int(r)))
        g_val = max(0, min(255, int(g)))
        b_val = max(0, min(255, int(b)))
        br_val = max(1, min(255, int(brightness)))
        return bytes([0x55, r_val, g_val, b_val, br_val, 0xFF, 0xF0])

    @staticmethod
    def off_btsnoop() -> bytes:
        """Turn off packet from btsnoop logs"""
        return bytes([0x55, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF0])

    @staticmethod
    def white_temp(level: int):
        """1-5  1-cold, 3-sunlight 5-warm
        1 - 6000K cold white
        2 - 5000K nature light
        3 - 4000K sun light
        4 - 3500K sun set
        5 - 3000K candle light
        """
        return Commands._std(Commands._cmd_white_temp, f"{level:02x}")

    @staticmethod
    def scene(scene: int):
        return Commands._rgb(Commands._cmd_scene, f"{scene:02x}ff32")


class ResponseStatus:
    def __init__(
        self,
        on: bool,
        brightness: int,
        temp_level: int | None,
        rgb: tuple[int, int, int],
    ):
        self.rgb = rgb
        self.on = on
        self.brightness = brightness
        self.temp_level = temp_level


class Response:
    _status_header = "55aa098815"
    """
       0 1 2 3 4  5 6 7  8 9 10 11 121314
      55aa098815 aaaaaa ffff ff 01 05ed6c
      0-4 header
      5-7 rgb
      8-9 white temp level 1-ff00, 2-b464 , 3-ffff, 4-4bc8, 5-00ff
      10  brightness
      11  on off
      12,13 white temp level 
      14  checksum ?
    """

    @staticmethod
    def is_status(response: bytearray | bytes):
        return response.hex().startswith(Response._status_header)

    @staticmethod
    def parse_status(response: bytearray | bytes) -> ResponseStatus | None:
        r = response[5]
        g = response[6]
        b = response[7]
        temp_level = None
        match f"{response[8]:02x}{response[9]:02x}":
            case "ff00":
                temp_level = 1
            case "b464":
                temp_level = 2
            case "ffff":
                temp_level = 3
            case "4bc8":
                temp_level = 4
            case "00ff":
                temp_level = 5
            case _:
                temp_level = None
        brightness = response[10]
        on = response[11] == 1
        return ResponseStatus(
            on,
            brightness,
            temp_level,
            (r, g, b),
        )
