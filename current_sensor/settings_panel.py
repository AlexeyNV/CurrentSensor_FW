"""Child-only settings: inline panel or a full page in the main window."""
from PySide6 import QtCore,QtGui,QtWidgets


class SettingsPage(QtWidgets.QWidget):
    def __init__(self,controls):
        super().__init__();self.cells=[];self.columns=0;self.available_width=900
        self.grid=QtWidgets.QGridLayout(self);self.grid.setContentsMargins(8,8,8,8)
        self.grid.setHorizontalSpacing(12);self.grid.setVerticalSpacing(8)
        for label,widget in controls:self.add_control(label,widget)

    def add_control(self,label,widget):
        cell=QtWidgets.QWidget();layout=QtWidgets.QVBoxLayout(cell)
        layout.setContentsMargins(0,0,0,0);layout.setSpacing(3)
        if label:
            title=QtWidgets.QLabel(label);title.setWordWrap(True);layout.addWidget(title)
        widget.setMinimumHeight(max(26,widget.minimumHeight()))
        if isinstance(widget,QtWidgets.QComboBox):
            widget.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            widget.setMinimumContentsLength(12)
            widget.view().setMinimumWidth(max(260,widget.view().sizeHintForColumn(0)+32))
        layout.addWidget(widget);widget.show()
        self.cells.append(cell);self.columns=0;self.prepare(self.available_width)

    def prepare(self,width):
        self.available_width=max(280,int(width))
        minimum=max([240]+[c.minimumSizeHint().width() for c in self.cells])
        columns=max(1,min(3,(self.available_width-16+12)//(minimum+12)))
        if columns==self.columns:return
        while self.grid.count():self.grid.takeAt(0)
        for col in range(3):self.grid.setColumnStretch(col,0)
        for i,cell in enumerate(self.cells):self.grid.addWidget(cell,i//columns,i%columns);cell.show()
        for col in range(columns):self.grid.setColumnStretch(col,1)
        self.columns=columns;self.updateGeometry()

    def required_height(self,width):
        self.prepare(width);cell_width=max(1,(width-16-12*(self.columns-1))//self.columns)
        heights=[]
        for cell in self.cells:
            layout=cell.layout();h=layout.totalHeightForWidth(cell_width) if layout.hasHeightForWidth() else layout.sizeHint().height()
            heights.append(max(h,cell.minimumSizeHint().height()))
        return 16+sum(max(heights[i:i+self.columns]) for i in range(0,len(heights),self.columns))+8*max(0,(len(heights)-1)//self.columns)


class SettingsTabs(QtWidgets.QTabWidget):
    def __init__(self):
        super().__init__();self.setUsesScrollButtons(True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Maximum)

    def required_size(self,width):
        page=self.currentWidget()
        height=page.required_height(max(280,width-4)) if page is not None else 0
        return QtCore.QSize(max(300,self.tabBar().minimumSizeHint().width()),height+self.tabBar().sizeHint().height()+6)

    def sizeHint(self):return self.required_size(max(300,self.width()))
    def minimumSizeHint(self):return self.required_size(max(300,self.width()))


class SettingsPanel(QtWidgets.QWidget):
    def __init__(self,window,tabs,button):
        super().__init__(window);self.window=window;self.tabs=tabs;self.button=button
        self.expanded=False;self.presentation='closed';self.changing=False;self.pending=False
        self.plot_ratio=.7;self.saved_docks=[]
        self.box=QtWidgets.QVBoxLayout(self);self.box.setContentsMargins(0,0,0,0)
        self.box.addWidget(tabs);self.box.addStretch()
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Maximum)
        self.tabs.currentChanged.connect(self.schedule)
        window.renderer.splitter.splitterMoved.connect(self.remember_plot_ratio)
        window.installEventFilter(self)
        self.escape=QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key.Key_Escape),self)
        self.escape.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.escape.activated.connect(lambda:button.setChecked(False))
        self.hide()

    def available_rect(self):return self.window.screen().availableGeometry()

    def sizeHint(self):return self.tabs.required_size(max(300,self.window.centralWidget().width()-18))
    def minimumSizeHint(self):return self.sizeHint() if self.expanded else QtCore.QSize(0,0)

    def set_expanded(self,expanded):
        if expanded and not self.expanded:
            sizes=self.window.renderer.splitter.sizes()
            if sum(sizes):self.plot_ratio=sizes[0]/sum(sizes)
        self.expanded=expanded
        if expanded:self.present()
        else:
            super().setVisible(False)
            self._restore_workspace()
            self.presentation='closed'
            self.window.centralWidget().updateGeometry()
            QtCore.QTimer.singleShot(0,self.restore_plot_ratio)

    def _restore_workspace(self):
        self.window.tabs.show()
        for dock in self.saved_docks:dock.show()
        self.saved_docks=[]
        self.window.centralWidget().layout().setStretchFactor(self,0)

    def restore_plot_ratio(self):
        if self.presentation=='page':return
        splitter=self.window.renderer.splitter
        total=max(1,splitter.height()-splitter.handleWidth())
        splitter.setSizes([int(total*self.plot_ratio),int(total*(1-self.plot_ratio))])

    def remember_plot_ratio(self,*_):
        if self.expanded:return
        sizes=self.window.renderer.splitter.sizes()
        if sum(sizes):self.plot_ratio=sizes[0]/sum(sizes)

    def eventFilter(self,obj,event):
        if obj is self.window and event.type()==QtCore.QEvent.Type.Resize:self.schedule()
        return False

    def schedule(self,*_):
        if self.expanded and not self.pending:
            self.pending=True;QtCore.QTimer.singleShot(0,self.present)

    def workspace_height(self):
        layout=self.window.centralWidget().layout()
        margins=layout.contentsMargins();used=margins.top()+margins.bottom();count=0
        for i in range(layout.count()):
            item=layout.itemAt(i)
            if item.widget() in (self,self.window.tabs):continue
            if item.isEmpty():continue
            used+=item.sizeHint().height();count+=1
        return max(1,self.window.centralWidget().height()-used-layout.spacing()*count)

    def present(self):
        self.pending=False
        if not self.expanded or self.changing:return
        self.changing=True
        try:
            # Re-evaluate against the normal workspace, not the previous page's
            # hidden plot height. This keeps resize/page changes deterministic.
            super().setVisible(False)
            self.setMinimumHeight(0);self.setMaximumHeight(16777215)
            self._restore_workspace()
            layout=self.window.centralWidget().layout();layout.activate()
            width=max(300,self.window.centralWidget().width()-18)
            needed=self.tabs.required_size(width)
            graph_min=max(450,self.window.tabs.minimumSizeHint().height())
            inline=self.workspace_height()-needed.height()>=graph_min
            if inline:
                self.presentation='inline'
                self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Maximum)
                self.setFixedHeight(needed.height())
            else:
                self.presentation='page'
                self.window.tabs.hide()
                self.saved_docks=[d for d in self.window.findChildren(QtWidgets.QDockWidget) if d.isVisible()]
                for dock in self.saved_docks:dock.hide()
                self.window.layout().activate();layout.activate()
                width=max(300,self.window.centralWidget().width()-18)
                needed=self.tabs.required_size(width)
                self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,QtWidgets.QSizePolicy.Policy.Expanding)
                self.setMinimumHeight(needed.height());self.setMaximumHeight(16777215)
                layout.setStretchFactor(self,1)
            self.tabs.setFixedHeight(needed.height())
            super().setVisible(True);self.updateGeometry();layout.activate()
            QtCore.QTimer.singleShot(0,self.restore_plot_ratio)
        finally:self.changing=False
