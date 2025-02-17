def classFactory(iface):
    from .auto_save import AutoSavePlugin
    return AutoSavePlugin(iface)
