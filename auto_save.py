# -*- coding: utf-8 -*-
import os
from functools import partial
from PyQt5.QtCore import QTimer, Qt, QSettings, QObject, QEvent, QTranslator, QLocale
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QAction,
    QMessageBox,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QPushButton,
    QCheckBox,
    QDockWidget,
)
from qgis.core import QgsProject, QgsApplication, QgsVectorLayer
from qgis.gui import (
    QgsMapToolCapture,
    QgsMapToolAdvancedDigitizing,
    QgsMapToolDigitizeFeature
)
from qgis.utils import iface


class SettingsDialog(QDialog):
    def __init__(self, current_save_interval, current_ask_save, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Auto Save Settings"))

        layout = QVBoxLayout()

        save_layout = QHBoxLayout()
        save_label = QLabel(self.tr("Save Prompt Interval (minutes):"))
        self.save_spinbox = QSpinBox()
        self.save_spinbox.setRange(1, 1440)
        self.save_spinbox.setValue(current_save_interval // 60000)
        save_layout.addWidget(save_label)
        save_layout.addWidget(self.save_spinbox)
        layout.addLayout(save_layout)

        self.ask_save_checkbox = QCheckBox(self.tr("Prompt to save the project and editing layers?"))
        self.ask_save_checkbox.setChecked(current_ask_save)
        layout.addWidget(self.ask_save_checkbox)

        buttons_layout = QHBoxLayout()
        ok_button = QPushButton(self.tr("OK"))
        cancel_button = QPushButton(self.tr("Cancel"))
        ok_button.clicked.connect(self.accept)
        cancel_button.clicked.connect(self.reject)
        buttons_layout.addStretch()
        buttons_layout.addWidget(ok_button)
        buttons_layout.addWidget(cancel_button)
        layout.addLayout(buttons_layout)

        self.setLayout(layout)

    def get_values(self):
        return (
            self.save_spinbox.value() * 60 * 1000,
            self.ask_save_checkbox.isChecked(),
        )


class AutoSavePlugin(QObject):
    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.settings = QSettings("YourCompany", "AutoSavePlugin")
        self.save_interval = self.settings.value("save_interval", 10 * 60 * 1000, type=int)
        self.ask_save = self.settings.value("ask_save", True, type=bool)

        # Debug key ("Y" to show messages in console, "N" to hide)
        self.debug_mode = "N"

        # Load translations and set fallback to English if not found
        self.translator = QTranslator()
        settings = QSettings()
        locale = settings.value("locale/userLocale", QLocale.system().name()).split('_')[0]
        locale_path = os.path.join(self.plugin_dir, 'i18n', f'auto_save_{locale}.qm')
        if os.path.exists(locale_path):
            self.translator.load(locale_path)
            QgsApplication.instance().installTranslator(self.translator)
        else:
            fallback_locale_path = os.path.join(self.plugin_dir, 'i18n', 'auto_save_en.qm')
            if os.path.exists(fallback_locale_path):
                self.translator.load(fallback_locale_path)
                QgsApplication.instance().installTranslator(self.translator)

        self.timer = QTimer()
        self.timer.timeout.connect(self.check_and_show_save_prompt)

        # Timer to check geometry modifications after vertex finishing
        self.post_vertex_timer = QTimer()
        self.post_vertex_timer.setSingleShot(True)
        self.post_vertex_timer.timeout.connect(self.check_geometry_modification)

        # State variables
        self.pending_save_prompt = False
        self.geometry_modified_since_prompt = False
        self.waiting_for_geometry_check = False

        self.iface.mapCanvas().mapToolSet.connect(self.on_map_tool_changed)
        self.current_map_tool = None

        QgsProject.instance().layersAdded.connect(self.on_layers_added)
        self.connected_layers = []

        # Install event filter for mouse events
        self.iface.mapCanvas().viewport().installEventFilter(self)
        self.vertex_count = 0

    def debug_print(self, message):
        """Shows messages in QGIS console only if debug_mode == 'Y'."""
        if self.debug_mode == "N":
            return
        python_console_dock = self.iface.mainWindow().findChild(QDockWidget, 'PythonConsole')
        if python_console_dock and python_console_dock.isVisible():
            print(message)

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        self.action = QAction(self.tr("Auto Save"), self.iface.mainWindow())
        self.action.setIcon(QIcon(icon_path))
        self.action.triggered.connect(self.open_settings_dialog)

        self.toolbar = self.iface.addToolBar(self.tr("AutoSaveToolbar"))
        self.toolbar.setObjectName("AutoSaveToolbar")
        self.toolbar.addAction(self.action)

        self.iface.addPluginToMenu(self.tr("&Auto Save"), self.action)

        self.timer.start(self.save_interval)

    def unload(self):
        self.iface.removePluginMenu(self.tr("&Auto Save"), self.action)
        self.toolbar.removeAction(self.action)
        self.iface.mainWindow().removeToolBar(self.toolbar)

        self.timer.stop()
        self.post_vertex_timer.stop()

        try:
            self.iface.mapCanvas().mapToolSet.disconnect(self.on_map_tool_changed)
        except:
            pass

        try:
            QgsProject.instance().layersAdded.disconnect(self.on_layers_added)
        except:
            pass

        for layer in self.connected_layers:
            self.disconnect_layer_signals(layer)
        self.connected_layers.clear()

        self.iface.mapCanvas().viewport().removeEventFilter(self)

    def on_layers_added(self, layers):
        for layer in layers:
            if isinstance(layer, QgsVectorLayer):
                if layer not in self.connected_layers:
                    self.connected_layers.append(layer)
                    layer.editingStarted.connect(partial(self.on_editing_started, layer))
                    layer.editingStopped.connect(partial(self.on_editing_stopped, layer))

    def on_editing_started(self, layer):
        """Called when editing starts for a layer."""
        if layer and isinstance(layer, QgsVectorLayer):
            layer.featureAdded.connect(partial(self.on_feature_added, layer))
            layer.geometryChanged.connect(partial(self.on_geometry_changed, layer))
            self.debug_print(self.tr(f"[AutoSavePlugin] {self.tr('Started editing')}: {layer.name()}"))

    def on_editing_stopped(self, layer):
        """Called when editing stops for a layer."""
        if layer and isinstance(layer, QgsVectorLayer):
            try:
                layer.featureAdded.disconnect(partial(self.on_feature_added, layer))
                layer.geometryChanged.disconnect(partial(self.on_geometry_changed, layer))
            except:
                pass
            self.debug_print(self.tr(f"[AutoSavePlugin] {self.tr('Stopped editing')}: {layer.name()}"))

    def disconnect_layer_signals(self, layer):
        """Disconnects all signals from a layer."""
        try:
            layer.editingStarted.disconnect(partial(self.on_editing_started, layer))
            layer.editingStopped.disconnect(partial(self.on_editing_stopped, layer))
        except:
            pass
        try:
            layer.featureAdded.disconnect(partial(self.on_feature_added, layer))
            layer.geometryChanged.disconnect(partial(self.on_geometry_changed, layer))
        except:
            pass

    def on_feature_added(self, layer, fid):
        """Called when a new feature is added."""
        tool_name = self.get_tool_name()
        self.debug_print(self.tr(f"[AutoSavePlugin] [{tool_name}] {self.tr('Geometry added or modified')} (FID: {fid}) {self.tr('in layer')}: {layer.name()}"))
        self.geometry_modified_since_prompt = True
        if self.waiting_for_geometry_check:
            self.post_vertex_timer.stop()
            self.waiting_for_geometry_check = False

        if self.pending_save_prompt:
            self.pending_save_prompt = False
            QTimer.singleShot(0, self.show_save_prompt)

    def on_geometry_changed(self, layer, fid, geom):
        """Called when geometry is changed."""
        tool_name = self.get_tool_name()
        if not geom or geom.isEmpty():
            self.debug_print(self.tr(f"[AutoSavePlugin] [{tool_name}] {self.tr('Failed to modify geometry')} (FID: {fid}) {self.tr('in layer')}: {layer.name()}"))
            if self.pending_save_prompt:
                self.pending_save_prompt = False
                QTimer.singleShot(0, self.show_save_prompt)
            return

        self.debug_print(self.tr(f"[AutoSavePlugin] [{tool_name}] {self.tr('Geometry added or modified')} (FID: {fid}) {layer.name()}"))
        self.geometry_modified_since_prompt = True
        if self.waiting_for_geometry_check:
            self.post_vertex_timer.stop()
            self.waiting_for_geometry_check = False

        if self.pending_save_prompt:
            self.pending_save_prompt = False
            QTimer.singleShot(0, self.show_save_prompt)

    def check_geometry_modification(self):
        """
        Called after a short time finishing vertices
        to check if there was a real geometry modification.
        """
        tool_name = self.get_tool_name()
        if not self.geometry_modified_since_prompt:
            self.debug_print(self.tr(f"[AutoSavePlugin] [{tool_name}] {self.tr('No geometry modification detected after finalizing vertices.')}"))
            if self.pending_save_prompt:
                self.pending_save_prompt = False
                self.show_save_prompt()

        self.waiting_for_geometry_check = False

    def on_map_tool_changed(self, new_tool, old_tool):
        self.current_map_tool = new_tool
        tool_name = self.get_tool_name()
        if new_tool:
            self.debug_print(self.tr(f"[AutoSavePlugin] {self.tr('Active tool')}: {tool_name}"))
            if self.pending_save_prompt and not self.is_creating_geometry():
                self.pending_save_prompt = False
                self.show_save_prompt()
        else:
            self.debug_print(self.tr(f"[AutoSavePlugin] {self.tr('No active tool')}"))

    def is_creating_geometry(self):
        """
        Checks if the current tool is capturing geometries
        (capture, advanced digitizing or digitize feature).
        """
        current_tool = self.iface.mapCanvas().mapTool()
        if isinstance(current_tool, QgsMapToolCapture):
            if hasattr(current_tool, 'captureCurve'):
                capture_curve = current_tool.captureCurve()
                if capture_curve and capture_curve.numPoints() > 0:
                    return True
            if hasattr(current_tool, 'points'):
                if len(current_tool.points()) > 0:
                    return True
        elif isinstance(current_tool, QgsMapToolAdvancedDigitizing):
            return True
        elif isinstance(current_tool, QgsMapToolDigitizeFeature):
            return True
        return False

    def check_and_show_save_prompt(self):
        """
        Called periodically by the main timer.
        """
        self.timer.stop()
        tool_name = self.get_tool_name()

        if self.pending_save_prompt:
            self.debug_print(self.tr(f"[AutoSavePlugin] [{tool_name}] {self.tr('A save prompt is already pending. Nothing to do now.')}"))
            self.timer.start(self.save_interval)
            return

        if self.is_creating_geometry():
            self.pending_save_prompt = True
            self.geometry_modified_since_prompt = False
            self.debug_print(self.tr(f"[AutoSavePlugin] [{tool_name}] {self.tr('Tool is in use. Save prompt is pending.')}"))
        else:
            self.show_save_prompt()

    def show_save_prompt(self):
        """
        Shows the save message or saves directly,
        depending on the user's settings.
        """
        if not self.ask_save:
            self.debug_print(self.tr("Prompt disabled. Saving automatically if needed."))
            self.save_project_and_layers(reopen=True)
            self.timer.start(self.save_interval)
            return

        reply = QMessageBox.question(
            self.iface.mainWindow(),
            self.tr("Auto Save"),
            self.tr("Do you want to save the project and editing layers?"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )

        if reply == QMessageBox.Yes:
            self.save_project_and_layers(reopen=True)

        self.timer.start(self.save_interval)

    def save_project_and_layers(self, reopen=True):
        """
        Saves all layers and/or the project only if changes exist.
        If reopen=True, reopens editing for layers that were in edit mode.
        Also restores the previously active tool after saving.
        """
        # Store current tool before saving
        current_tool = self.iface.mapCanvas().mapTool()

        project = QgsProject.instance()
        project_changed = project.isDirty()  # True if there's something to write

        layers = project.mapLayers().values()
        editing_layers = [
            layer for layer in layers
            if isinstance(layer, QgsVectorLayer) and layer.isEditable()
        ]

        changed_layers = []
        for layer in editing_layers:
            # isModified() should be True if there are uncommitted changes
            if layer.isModified():
                changed_layers.append(layer)

        # If no changes, do nothing
        if not project_changed and not changed_layers:
            self.debug_print(self.tr("[AutoSavePlugin] No changes in project or layers. Nothing to save."))
            return

        # Save only layers that changed
        if changed_layers:
            layers_to_reopen = []
            for layer in changed_layers:
                layers_to_reopen.append(layer)
                layer.commitChanges()
                layer.triggerRepaint()
                self.debug_print(self.tr("[AutoSavePlugin] Changes saved in layer: {layer_name}").format(layer_name=layer.name()))

            # If reopen is True, start editing again
            if reopen:
                for layer in layers_to_reopen:
                    layer.startEditing()
                    self.debug_print(self.tr("[AutoSavePlugin] Editing reopened in layer: {layer_name}").format(layer_name=layer.name()))

        # Save project only if it changed
        if project_changed:
            project.write()
            self.debug_print(self.tr("[AutoSavePlugin] Project saved."))

        # Restore the previously active tool, if any
        if current_tool:
            self.iface.mapCanvas().setMapTool(current_tool)

    def open_settings_dialog(self):
        """Opens the plugin settings dialog."""
        dialog = SettingsDialog(self.save_interval, self.ask_save, self.iface.mainWindow())
        if dialog.exec_() == QDialog.Accepted:
            new_save_interval, new_ask_save = dialog.get_values()
            self.save_interval = new_save_interval
            self.ask_save = new_ask_save

            self.timer.stop()
            self.timer.start(self.save_interval)

            self.settings.setValue("save_interval", self.save_interval)
            self.settings.setValue("ask_save", self.ask_save)
            self.debug_print(self.tr("[AutoSavePlugin] Settings updated."))

    def eventFilter(self, watched, event):
        """
        Captures mouse click events to count added or finalized vertices.
        """
        if event.type() == QEvent.MouseButtonPress and self.current_map_tool:
            tool_name = self.get_tool_name()
            pos = event.pos()

            if isinstance(self.current_map_tool, (QgsMapToolCapture, QgsMapToolAdvancedDigitizing, QgsMapToolDigitizeFeature)):
                if event.button() == Qt.LeftButton:
                    self.vertex_count += 1
                    self.debug_print(self.tr("[AutoSavePlugin] [{tool_name}] Adding vertex #{vertex} at position: ({x}, {y})").format(tool_name=tool_name, vertex=self.vertex_count, x=pos.x(), y=pos.y()))
                elif event.button() == Qt.RightButton:
                    self.debug_print(self.tr("[AutoSavePlugin] [{tool_name}] Finalizing vertex addition at position: ({x}, {y})").format(tool_name=tool_name, x=pos.x(), y=pos.y()))
                    if not isinstance(self.current_map_tool, QgsMapToolDigitizeFeature):
                        self.waiting_for_geometry_check = True
                        self.geometry_modified_since_prompt = False
                        self.post_vertex_timer.start(500)

        return False

    def get_tool_name(self):
        if self.current_map_tool:
            return type(self.current_map_tool).__name__
        return self.tr("No active tool")


def classFactory(iface):
    return AutoSavePlugin(iface)
