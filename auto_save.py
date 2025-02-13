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

        # Carregar as traduções
        self.translator = QTranslator()
        locale = QLocale.system().name()
        locale_path = os.path.join(self.plugin_dir, 'i18n', f'auto_save_{locale}.qm')
        if os.path.exists(locale_path):
            self.translator.load(locale_path)
            QgsApplication.instance().installTranslator(self.translator)

        self.timer = QTimer()
        self.timer.timeout.connect(self.check_and_show_save_prompt)

        # Timer to check if there was an actual modification after finalizing vertices.
        self.post_vertex_timer = QTimer()
        self.post_vertex_timer.setSingleShot(True)
        self.post_vertex_timer.timeout.connect(self.check_geometry_modification)

        # State control variables
        self.pending_save_prompt = False               # If there is a prompt pending to display
        self.geometry_modified_since_prompt = False    # If there was a geometry modification since the last prompt
        self.waiting_for_geometry_check = False        # If we are waiting for post-vertex finalization check

        self.iface.mapCanvas().mapToolSet.connect(self.on_map_tool_changed)
        self.current_map_tool = None

        QgsProject.instance().layersAdded.connect(self.on_layers_added)
        self.connected_layers = []

        # Filter to capture mouse events on the canvas
        self.iface.mapCanvas().viewport().installEventFilter(self)
        self.vertex_count = 0

    def debug_print(self, message):
        """Prints to the QGIS console, if open."""
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

        self.iface.addPluginToMenu("&Auto Save", self.action)

        self.timer.start(self.save_interval)

    def unload(self):
        self.iface.removePluginMenu("&Auto Save", self.action)
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
        if layer and isinstance(layer, QgsVectorLayer):
            layer.featureAdded.connect(partial(self.on_feature_added, layer))
            layer.geometryChanged.connect(partial(self.on_geometry_changed, layer))
            self.debug_print(f"[AutoSavePlugin] {self.tr('Started editing')}: {layer.name()}")

    def on_editing_stopped(self, layer):
        if layer and isinstance(layer, QgsVectorLayer):
            try:
                layer.featureAdded.disconnect(partial(self.on_feature_added, layer))
                layer.geometryChanged.disconnect(partial(self.on_geometry_changed, layer))
            except:
                pass
            self.debug_print(f"[AutoSavePlugin] {self.tr('Stopped editing')}: {layer.name()}")

    def disconnect_layer_signals(self, layer):
        """Disconnects all signals of a layer."""
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
        self.debug_print(f"[AutoSavePlugin] [{tool_name}] {self.tr('Geometry added or modified')} (FID: {fid}) {self.tr('in layer')}: {layer.name()}")
        self.geometry_modified_since_prompt = True
        if self.waiting_for_geometry_check:
            self.post_vertex_timer.stop()
            self.waiting_for_geometry_check = False

        # If there was a pending prompt, display it now
        if self.pending_save_prompt:
            self.pending_save_prompt = False
            QTimer.singleShot(0, self.show_save_prompt)

    def on_geometry_changed(self, layer, fid, geom):
        """Called when the geometry is changed."""
        tool_name = self.get_tool_name()
        if not geom or geom.isEmpty():
            self.debug_print(f"[AutoSavePlugin] [{tool_name}] {self.tr('Failed to modify geometry')} (FID: {fid}) {self.tr('in layer')}: {layer.name()}")
            # If there was a pending prompt, display it now, but there is no valid geometry
            if self.pending_save_prompt:
                self.pending_save_prompt = False
                QTimer.singleShot(0, self.show_save_prompt)
            return

        self.debug_print(f"[AutoSavePlugin] [{tool_name}] {self.tr('Geometry added or modified')} (FID: {fid}) {layer.name()}")
        self.geometry_modified_since_prompt = True
        if self.waiting_for_geometry_check:
            self.post_vertex_timer.stop()
            self.waiting_for_geometry_check = False

        if self.pending_save_prompt:
            self.pending_save_prompt = False
            QTimer.singleShot(0, self.show_save_prompt)

    def check_geometry_modification(self):
        """
        Called a few milliseconds after finalizing vertices to check
        if there was actually any geometry modification. If not and there is a pending prompt,
        display the save prompt.
        """
        tool_name = self.get_tool_name()
        # If no geometry modification was detected
        if not self.geometry_modified_since_prompt:
            self.debug_print(f"[AutoSavePlugin] [{tool_name}] {self.tr('No geometry modification detected after finalizing vertices.')}")
            # If the save prompt is pending, show it
            if self.pending_save_prompt:
                self.pending_save_prompt = False
                self.show_save_prompt()

        # Always reset the geometry check
        self.waiting_for_geometry_check = False

    def on_map_tool_changed(self, new_tool, old_tool):
        self.current_map_tool = new_tool
        tool_name = self.get_tool_name()
        if new_tool:
            self.debug_print(f"[AutoSavePlugin] {self.tr('Active tool')}: {tool_name}")
            # If there was a pending prompt and the new tool is not creating geometry, display the prompt
            if self.pending_save_prompt and not self.is_creating_geometry():
                self.pending_save_prompt = False
                self.show_save_prompt()
        else:
            self.debug_print(f"[AutoSavePlugin] {self.tr('No active tool')}")

    def is_creating_geometry(self):
        """
        Checks if the current tool is capture, advanced digitizing,
        or digitize feature, and if it is in the process of creating geometry.
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
        Called periodically by the main timer. If the user is creating
        geometry, mark a pending prompt. Otherwise, call the prompt directly.
        """
        self.timer.stop()
        tool_name = self.get_tool_name()

        # If there is already a pending prompt, do not call another to avoid duplication.
        if self.pending_save_prompt:
            self.debug_print(f"[AutoSavePlugin] [{tool_name}] {self.tr('A save prompt is already pending. Nothing to do now.')}")
            self.timer.start(self.save_interval)
            return

        if self.is_creating_geometry():
            self.pending_save_prompt = True
            self.geometry_modified_since_prompt = False
            self.debug_print(f"[AutoSavePlugin] [{tool_name}] {self.tr('Tool is in use. Save prompt is pending.')}")
        else:
            self.show_save_prompt()

    def show_save_prompt(self):
        """
        Displays the save message or saves directly, according to
        the user's configuration. Restarts the main timer at the end.
        """
        # If the save prompt is disabled, save automatically
        if not self.ask_save:
            self.debug_print(f"[AutoSavePlugin] {self.tr('Prompt disabled. Saving automatically.')}")
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

        # At the end, reactivate the main timer
        self.timer.start(self.save_interval)

    def save_project_and_layers(self, reopen=True):
        """
        Saves all editing layers and the project. If reopen=True,
        reopens the layers in editing mode.
        """
        current_tool = self.iface.mapCanvas().mapTool()
        project = QgsProject.instance()
        layers = project.mapLayers().values()
        editing_layers = [layer for layer in layers if isinstance(layer, QgsVectorLayer) and layer.isEditable()]
        layers_to_reopen = []

        for layer in editing_layers:
            if layer.isEditable():
                if reopen:
                    layers_to_reopen.append(layer)
                layer.commitChanges()
                layer.triggerRepaint()
                self.debug_print(f"[AutoSavePlugin] {self.tr('Changes saved in layer')}: {layer.name()}")

        project.write()
        self.debug_print(f"[AutoSavePlugin] {self.tr('Project saved.')}")

        if reopen:
            for layer in layers_to_reopen:
                layer.startEditing()
                self.debug_print(f"[AutoSavePlugin] {self.tr('Editing reopened in layer')}: {layer.name()}")

        # Restore the current tool, if any
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
            self.debug_print(f"[AutoSavePlugin] {self.tr('Settings updated.')}")

    def eventFilter(self, watched, event):
        """
        Captures mouse click events to check when the user
        adds vertices (left click) or finalizes geometry (right click).
        """
        if event.type() == QEvent.MouseButtonPress and self.current_map_tool:
            tool_name = self.get_tool_name()
            pos = event.pos()

            # If the current tool is digitizing/capturing
            if isinstance(self.current_map_tool, (QgsMapToolCapture, QgsMapToolAdvancedDigitizing, QgsMapToolDigitizeFeature)):
                if event.button() == Qt.LeftButton:
                    self.vertex_count += 1
                    self.debug_print(f"[AutoSavePlugin] [{tool_name}] {self.tr('Adding vertex')} #{self.vertex_count} {self.tr('at position')}: ({pos.x()}, {pos.y()})")
                elif event.button() == Qt.RightButton:
                    self.debug_print(f"[AutoSavePlugin] [{tool_name}] {self.tr('Finalizing vertex addition')} {self.tr('at position')}: ({pos.x()}, {pos.y()})")
                    # If the tool is not QgsMapToolDigitizeFeature, wait for post-finalization check
                    if not isinstance(self.current_map_tool, QgsMapToolDigitizeFeature):
                        self.waiting_for_geometry_check = True
                        self.geometry_modified_since_prompt = False
                        # Start a 500ms timer to check if there was a modification
                        self.post_vertex_timer.start(500)

        return False

    def get_tool_name(self):
        if self.current_map_tool:
            return type(self.current_map_tool).__name__
        return self.tr("No active tool")


def classFactory(iface):
    return AutoSavePlugin(iface)
