"""実機（Raspberry Pi と Robot HAT）のアダプタ。

ハードのライブラリ（smbus2、lgpio、numpy）は、各モジュールの関数の中でだけ読み込む。
"""

from osekkai.adapters.pi.devices import PiFactories, build_pi_devices, build_pi_voice_devices

__all__ = ["PiFactories", "build_pi_devices", "build_pi_voice_devices"]
