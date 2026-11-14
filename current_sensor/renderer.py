"""Replaceable plotting surface; numeric envelopes live in data.py."""
from typing import Protocol
from collections import deque
import time
import numpy as np
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg
from .data import EnvelopeIndex, cursor_metrics,robust_y_range


class Renderer(Protocol):
    def draw(self, traces, anomalies): ...
    def set_overview(self, data): ...


class ScopeViewBox(pg.ViewBox):
    def wheelEvent(self, event, axis=None):
        modifiers = event.modifiers()
        y_only = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        both = bool(modifiers & QtCore.Qt.KeyboardModifier.ControlModifier)
        factor = 1.02 ** (-event.delta() / 8)
        center = self.mapSceneToView(event.scenePos())
        self.scaleBy(x=factor if not y_only else 1,
                     y=factor if y_only or both else 1, center=center)
        self.sigRangeChangedManually.emit([not y_only, y_only or both])
        event.accept()


class MeasuredPlotWidget(pg.PlotWidget):
    """Bounded paint telemetry, independent of received sample timestamps."""
    def __init__(self, *args, **kwargs):
        self.paint_times = deque(maxlen=2000)
        self.paint_count = 0
        super().__init__(*args, **kwargs)

    def paintEvent(self, event):
        started = time.perf_counter()
        super().paintEvent(event)
        self.paint_times.append((time.perf_counter()-started)*1000)
        self.paint_count += 1


class PyQtGraphRenderer(QtWidgets.QWidget):
    manual_view = QtCore.Signal()
    cursor_changed = QtCore.Signal(dict)
    region_changed = QtCore.Signal(float, float)
    anomaly_clicked = QtCore.Signal(float, float)
    overview_event_clicked=QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=False, background="#101820", foreground="#cdd9e5")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = ScopeViewBox()
        self.plot = MeasuredPlotWidget(viewBox=self.view)
        self.plot.setLabel("bottom", "Время от первого отсчёта", units="s")
        self.plot.setLabel("left", "Ток RAW", units="mA")
        self.plot.getAxis('left').enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=.18)
        self.plot.addLegend(offset=(12, 10))
        self.curves = [self.plot.plot(name="RAW", pen=pg.mkPen("#52dfc5", width=1)),
                       self.plot.plot(name="MEDIAN", pen=pg.mkPen("#ffc66d", width=1))]
        for curve in self.curves:
            curve.setClipToView(True)
            # The custom first/min/max/last envelope preserves original times.
            # A second automatic reduction would move extrema and merge gaps.
            curve.setDownsampling(ds=1, auto=False, method="peak")
        self.markers = pg.ScatterPlotItem(size=9, pen="#ff647c", brush="#ff647c", symbol="t")
        self.plot.addItem(self.markers)
        self.markers.sigClicked.connect(self._marker_click)
        self.overview = MeasuredPlotWidget()
        self.following = False
        self.next_overview_region = 0.0
        self.overview_region_period = .2
        self.overview.setMinimumHeight(55)
        self.overview.setLabel("bottom", "Обзор доступной истории", units="s")
        self.overview.getAxis('left').enableAutoSIPrefix(False)
        self.overview_curve = self.overview.plot(pen=pg.mkPen("#668d9d", width=1))
        self.overview_markers=pg.ScatterPlotItem(size=9,pen='#ffc66d',brush='#ffc66d',symbol='t')
        self.overview.addItem(self.overview_markers)
        self.overview_markers.sigClicked.connect(lambda _,points:self.overview_event_clicked.emit(points[0].data()) if points else None)
        self.overview_mode='Robust Y';self.overview_trace=None;self.overview_events=[]
        self.overview_bounds=None;self.overview_limits=None
        self.window_region = pg.LinearRegionItem(values=(0, 1), brush=(82, 223, 197, 35))
        self.overview.addItem(self.window_region)
        self.window_region.sigRegionChangeFinished.connect(self._overview_changed)
        self.overview_container=QtWidgets.QWidget();overview_layout=QtWidgets.QVBoxLayout(self.overview_container)
        overview_layout.setContentsMargins(0,0,0,0);overview_layout.setSpacing(2)
        self.overview_controls=QtWidgets.QHBoxLayout();overview_layout.addLayout(self.overview_controls)
        overview_layout.addWidget(self.overview,1)
        self.splitter=QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.plot);self.splitter.addWidget(self.overview_container)
        self.splitter.setStretchFactor(0,3);self.splitter.setStretchFactor(1,1)
        self.splitter.setSizes([420,120])
        layout.addWidget(self.splitter,1)
        self.view.sigRangeChangedManually.connect(lambda *_: self.manual_view.emit())
        self.view.sigXRangeChanged.connect(self._range_changed)
        self.x_cursors = [pg.InfiniteLine(pos=p, angle=90, movable=True, pen="#bc9cff") for p in (.1, .2)]
        self.y_cursors = [pg.InfiniteLine(pos=p, angle=0, movable=True, pen="#87b7ff") for p in (0, 100)]
        for cursor in self.x_cursors + self.y_cursors:
            self.plot.addItem(cursor)
            cursor.hide()
            cursor.sigPositionChanged.connect(self._cursors_changed)
        self.roi = pg.LinearRegionItem(values=(.1, .2), brush=(188, 156, 255, 25))
        self.plot.addItem(self.roi)
        self.roi.hide()
        self.cursor_region = pg.LinearRegionItem(values=(.1, .2), movable=False,
                                                 brush=(188, 156, 255, 18), pen=pg.mkPen(None))
        self.cursor_region.setZValue(-5)
        self.plot.addItem(self.cursor_region)
        self.cursor_region.hide()
        self.roi.sigRegionChangeFinished.connect(lambda: self.region_changed.emit(*self.roi.getRegion()))
        self.overview_index = None
        self.trace_names = None

    def set_trace_names(self, names):
        if self.trace_names == names:
            return
        self.trace_names = names
        legend = self.plot.plotItem.legend
        legend.clear()
        for i, curve in enumerate(self.curves):
            curve.setVisible(i < len(names))
            if i < len(names):
                legend.addItem(curve, names[i])
        self.plot.setLabel('left', 'Ток ' + ' + '.join(names), units=getattr(self,'current_unit','mA'))

    def draw(self, traces, anomalies, scale=1.0, shift=0.0):
        for curve, trace in zip(self.curves, traces):
            curve.setData(trace.x, trace.y * scale + shift, connect=trace.connect,
                          symbol="o" if not trace.aggregated and len(trace.x) <= 100 else None,
                          symbolSize=4)
        for curve in self.curves[len(traces):]:
            curve.setData([], [])
        if len(anomalies):
            low, high = self.view.viewRange()[1]
            actual = anomalies["vshunt_code"].astype(float) / 16 * scale + shift
            shown = np.clip(actual, low + (high - low) * .015, high - (high - low) * .015)
            self.markers.setData(x=anomalies["time_s"], y=shown,
                                 data=list(zip(anomalies["time_s"], actual)))
        else:
            self.markers.setData([], [])

    def set_overview(self, data, index=None):
        if not len(data):
            return
        if self.overview_index is not None and self.overview_index.data is data:
            return
        self.overview_index = index or EnvelopeIndex(data)
        trace = self.overview_index.query(data["time_s"][0], data["time_s"][-1], max(200, self.width()))
        self.set_overview_trace(trace,float(data['time_s'][0]),float(data['time_s'][-1]),robust_y_range(data['vshunt_code']))

    def set_overview_trace(self,trace,left,right,robust_bounds=None):
        self.overview_trace=trace;self.overview_limits=(left,right);self.overview_bounds=robust_bounds
        self._paint_overview()
        self.overview.setXRange(left,max(right,left+.001),padding=0)

    def set_overview_mode(self,mode):
        self.overview_mode=mode;self._paint_overview()

    def set_overview_bounds(self,bounds):
        self.overview_bounds=bounds;self._paint_overview()

    def set_overview_events(self,events):
        if self.overview_limits is None:return
        left,right=self.overview_limits
        self.overview_events=[e for e in events if left<=e['time_s']<=right]
        self._paint_overview()

    def _paint_overview(self):
        trace=self.overview_trace
        if trace is None or not len(trace.x):return
        factor=.001 if getattr(self,'current_unit','mA')=='A' else 1.
        self.overview_curve.setData(trace.x,trace.y*factor,connect=trace.connect)
        if self.overview_mode=='Full RAW':
            low,high=float(trace.y.min()),float(trace.y.max());pad=max(1.,high-low)*.08
            low,high=low-pad,high+pad
        else:
            low,high=self.overview_bounds if self.overview_bounds is not None else robust_y_range(trace.y*16)
        self.overview.setYRange(low*factor,high*factor,padding=0)
        self.overview.setLabel('left',self.overview_mode,units=getattr(self,'current_unit','mA'))
        events=self.overview_events
        self.overview_markers.setData(x=[e['time_s'] for e in events],
            y=[min(high-(high-low)*.03,max(low+(high-low)*.03,e['current_ma']))*factor for e in events],data=events)

    def _overview_changed(self):
        self.view.setXRange(*self.window_region.getRegion(), padding=0)
        self.manual_view.emit()

    def _range_changed(self, _view, bounds):
        now = time.monotonic()
        if self.following and now < self.next_overview_region:
            return
        self.next_overview_region = now + self.overview_region_period
        self.window_region.blockSignals(True)
        self.window_region.setRegion(bounds)
        self.window_region.blockSignals(False)

    def set_follow_mode(self, enabled):
        self.following = enabled
        if not enabled:
            self._range_changed(self.view, self.view.viewRange()[0])

    def _cursors_changed(self):
        if hasattr(self, 'cursor_region'):
            self.cursor_region.setRegion(sorted(line.value() for line in self.x_cursors))
        self.cursor_changed.emit(cursor_metrics(*(line.value() for line in self.x_cursors + self.y_cursors)))

    def show_cursors(self, enabled):
        self.cursor_region.setVisible(enabled)
        for cursor in self.x_cursors + self.y_cursors:
            cursor.setVisible(enabled)
        if enabled:
            x, y = self.view.viewRange()
            for lines, bounds in ((self.x_cursors, x), (self.y_cursors, y)):
                for line, fraction in zip(lines, (.25, .75)):
                    line.setValue(bounds[0] + (bounds[1] - bounds[0]) * fraction)
            self._cursors_changed()

    def _marker_click(self, _item, points):
        if points:
            self.anomaly_clicked.emit(*points[0].data())


class VisPyRenderer(QtWidgets.QWidget):
    """Experimental benchmark backend; GUI remains on PyQtGraph until measured."""
    def __init__(self, parent=None):
        super().__init__(parent)
        from vispy import app, scene
        app.use_app("pyside6")
        self.canvas = scene.SceneCanvas(keys=None, bgcolor="#101820", size=(1000, 600))
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.canvas.native)
        self.view = self.canvas.central_widget.add_view()
        self.view.camera = "panzoom"
        self.lines = [scene.Line(parent=self.view.scene, color=color, method="gl")
                      for color in ("#52dfc5", "#ffc66d")]

    def draw(self, traces, anomalies=None):
        for line, trace in zip(self.lines, traces):
            positions = np.column_stack((trace.x, trace.y)).astype(np.float32)
            pairs = np.column_stack((np.flatnonzero(trace.connect), np.flatnonzero(trace.connect) + 1)).astype(np.uint32)
            line.set_data(pos=positions, connect=pairs)
        self.canvas.update()

    def set_overview(self, data):
        pass
