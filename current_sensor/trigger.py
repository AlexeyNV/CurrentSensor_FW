"""Host RAW software trigger. It never controls the ADC, USB or recorder."""
from dataclasses import dataclass
import numpy as np


@dataclass
class TriggerConfig:
    mode: str = "Off"
    edge: str = "Rising"
    level_ma: float = 150.0
    hysteresis_ma: float = 2.0
    pre_s: float = .1
    post_s: float = .2
    holdoff_s: float = .5

    def validate(self):
        if self.mode not in ("Off", "Auto", "Normal", "Single") or self.edge not in ("Rising", "Falling", "Level"):
            raise ValueError("invalid trigger mode/edge")
        if not all(np.isfinite(x) for x in (self.level_ma, self.hysteresis_ma, self.pre_s, self.post_s, self.holdoff_s)):
            raise ValueError("non-finite trigger parameter")
        if min(self.hysteresis_ma, self.pre_s, self.post_s, self.holdoff_s) < 0:
            raise ValueError("negative trigger duration/hysteresis")


class SoftwareTrigger:
    def __init__(self, config=None):
        self.configure(config or TriggerConfig())

    def configure(self, config):
        config.validate()
        self.config = config
        self.pending = None
        self.armed_edge = False
        self.enabled = config.mode != "Off"
        self.last_slot = None
        self.next_allowed = 0.0
        self.last_event = 0.0

    def process(self, data):
        if not self.enabled or not len(data):
            return None
        cfg = self.config
        completed = None
        for sample in data:
            stamp = float(sample["time_s"])
            slot = int(sample["slot"])
            value = int(sample["vshunt_code"]) / 16
            if self.last_slot is None or (slot - self.last_slot) & 0xFFFFFFFF != 1:
                self.armed_edge = False
                self.pending = None
            self.last_slot = slot
            if self.pending is not None:
                if stamp >= self.pending["time_s"] + cfg.post_s:
                    event = self.pending
                    self.pending = None
                    self.next_allowed = stamp + cfg.holdoff_s
                    self.last_event = stamp
                    if cfg.mode == "Single":
                        self.enabled = False
                        return event
                    completed = event
                continue
            if stamp < self.next_allowed:
                continue
            if cfg.edge == "Rising" and value <= cfg.level_ma - cfg.hysteresis_ma:
                self.armed_edge = True
            if cfg.edge == "Falling" and value >= cfg.level_ma + cfg.hysteresis_ma:
                self.armed_edge = True
            crossed = (cfg.edge == "Level" and value >= cfg.level_ma or
                       cfg.edge == "Rising" and self.armed_edge and value >= cfg.level_ma or
                       cfg.edge == "Falling" and self.armed_edge and value <= cfg.level_ma)
            automatic = cfg.mode == "Auto" and stamp - self.last_event >= max(1.0, cfg.pre_s + cfg.post_s)
            if crossed or automatic:
                self.pending = dict(time_s=stamp, slot=slot, triggered=bool(crossed),
                                    pre_s=cfg.pre_s, post_s=cfg.post_s)
                self.armed_edge = False
                if cfg.post_s == 0:
                    completed, self.pending = self.pending, None
                    self.next_allowed = stamp + cfg.holdoff_s
                    self.last_event = stamp
                    if cfg.mode == "Single":
                        self.enabled = False
                        return completed
        return completed
