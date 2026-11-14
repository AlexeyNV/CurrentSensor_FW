"""Read-only USB enumeration, independent of the serial reader and Qt."""
from datetime import datetime,timezone
import threading,time
from serial.tools import list_ports


class ConnectionMonitor(threading.Thread):
    def __init__(self,port,callback,interval=.1):
        super().__init__(name='CurrentSensor USB presence',daemon=True)
        self.port=port;self.callback=callback;self.interval=interval
        self.stopping=threading.Event();self.history=[];self.latest=None;self.start()

    def run(self):
        previous=None
        while not self.stopping.is_set():
            try:
                ports=[dict(port=p.device,vid=p.vid,pid=p.pid,serial=p.serial_number)
                    for p in list_ports.comports() if p.device==self.port or (p.vid,p.pid)==(0x483,0x5740)]
                signature=repr(ports)
                if signature!=previous:
                    event=dict(monotonic=time.monotonic(),utc=datetime.now(timezone.utc).isoformat(),
                        port=self.port,present=any(p['port']==self.port for p in ports),devices=ports)
                    self.history.append(event);self.latest=event;self.callback(event);previous=signature
            except Exception as error:
                if previous!='ENUM_ERROR':self.callback(dict(error=str(error),port=self.port));previous='ENUM_ERROR'
            self.stopping.wait(self.interval)

    def finish(self):self.stopping.set()
