"""基盤の設定。いまは声の設定だけ（ほかの項目はタスク 2.2 で足す）。"""

from pathlib import Path

from pydantic import BaseModel, Field


class VoiceSettings(BaseModel):
    """声の合成と再生の設定。"""

    open_jtalk: str = "open_jtalk"
    dictionary: Path = Path("/var/lib/mecab/dic/open-jtalk/naist-jdic")
    htsvoice: Path = Path("~/.local/share/osekkai/voices/tohoku-f01-happy.htsvoice")
    pitch: float = Field(default=3.0, ge=-24, le=24)
    speed: float = Field(default=1.05, ge=0.5, le=2.0)
    sink: str = ""  # 空なら既定の再生先
