# Auto Save Plugin for QGIS

The **Auto Save** plugin automatically saves your QGIS project and editable layers at a user-defined interval, helping prevent data loss and streamlining your editing workflow.

## Features
- Configurable save interval
- Option to prompt for confirmation or save automatically
- Detects and finalizes geometry editing before saving
- Restores the previously active map tool after saving

## Installation
1. Download or clone this repository.
2. Place the `Auto_save` folder in your QGIS plugins directory.
3. Restart QGIS and enable the plugin in the Plugin Manager.

## Usage
1. Set your preferred save interval in the plugin settings.
2. Choose whether to be prompted before each save or to save silently.
3. The plugin will take care of saving at the chosen interval, waiting for edits to finalize.

## Contributing
- Submit issues or pull requests on [GitHub](https://github.com/joaobrafor/Auto_save/issues).

## License
[MIT License](LICENSE).
