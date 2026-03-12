import os
import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("PDFMergePro")
    app.setApplicationName("PDF Merge Pro")

    window = MainWindow()

    # Optional app icon support: drop an .ico file into assets/app.ico
    icon_path = os.path.join(os.path.dirname(__file__), "assets", "app.ico")
    if os.path.exists(icon_path):
        window.setWindowIcon(QIcon(icon_path))

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
