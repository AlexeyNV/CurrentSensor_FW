"""Coalesced cursor/ROI calculations; Qt never builds a large range index."""
import threading
from .range_stats import RawRangeIndex
from .archive import ArchiveReader
from .analysis_statistics import panel_statistics,nearest_sample,rolling_statistics,overview_robust_bounds


class StatisticsWorker(threading.Thread):
    def __init__(self, events):
        super().__init__(name='CurrentSensor range statistics', daemon=True)
        self.events = events
        self.condition = threading.Condition()
        self.pending = {}
        self.stopping = False
        self.cached_data = self.cached_index = None
        self.start()

    def submit(self, role, token, source, x1, x2):
        with self.condition:
            self.pending[role] = (token, source, x1, x2)
            self.condition.notify()

    def finish(self):
        with self.condition:
            self.stopping = True
            self.pending.clear()
            self.condition.notify()

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.stopping)
                if self.stopping:
                    return
                role = next(iter(self.pending))
                token, source, x1, x2 = self.pending.pop(role)
            try:
                if role=='overview':
                    result=overview_robust_bounds(source)
                    self.events.put(('range_stats',(role,token,result,None)))
                    continue
                if role=='rolling':
                    result=rolling_statistics(source,x1,x2)
                    self.events.put(('range_stats',(role,token,result,None)))
                    continue
                if role=='panel':
                    result=panel_statistics(source,x1,x2)
                    self.events.put(('range_stats',(role,token,result,None)))
                    continue
                cursor_samples=[nearest_sample(source,t) for t in (x1,x2)] if role=='cursor' else None
                if cursor_samples and all(cursor_samples):
                    x1,x2=(sample['time_s'] for sample in cursor_samples)
                if isinstance(source.data,ArchiveReader):
                    result=source.data.range_stats(x1,x2)
                    if source.analyzer is not None:
                        result['filtered']=source.data.filtered_range_stats(
                            x1,x2,source.analyzer,result)
                    result.update(source_kind='ARCHIVE',source_history_complete=source.full_available)
                    result['cursor_samples']=cursor_samples
                    self.events.put(('range_stats',(role,token,result,None)))
                    continue
                data = source.raw()
                index = source.index
                if index is None:
                    if data is not self.cached_data:
                        self.cached_index = RawRangeIndex.from_data(data)
                        self.cached_data = data
                    index = self.cached_index
                result = index.query(data, x1, x2)
                if source.analyzer is not None:
                    result['filtered'] = source.analyzer.range_statistics(
                        data,x1,x2,source.base_index)
                result.update(source_kind=source.source_kind, source_history_complete=source.full_available)
                result['cursor_samples']=cursor_samples
                self.events.put(('range_stats', (role, token, result, None)))
            except Exception as error:
                self.events.put(('range_stats', (role, token, None, str(error))))
