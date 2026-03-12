import os
from dataclasses import dataclass
from typing import List

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QSettings
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from core.pdf_merger import PdfMergerService
from utils.file_utils import (
    format_bytes,
    format_modified,
    get_file_modified_timestamp,
    get_file_size_bytes,
    get_pdf_page_count,
    is_pdf_file,
    normalize_path,
    unique_pdf_paths,
)


@dataclass
class PdfItem:
    path: str
    pages: int
    size_bytes: int
    modified_ts: float


class FileListWidget(QTableWidget):
    external_files_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("DropArea")
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["Name", "Size", "Pages", "Modified"])
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(True)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, self.horizontalHeader().ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, self.horizontalHeader().ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, self.horizontalHeader().ResizeMode.ResizeToContents)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
        self.setSortingEnabled(True)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)

        self._placeholder = QLabel("Drag and drop PDF files here", self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setObjectName("DropLabel")
        self._placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._update_placeholder()

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        self._placeholder.resize(self.viewport().size())

    def _update_placeholder(self) -> None:
        self._placeholder.setVisible(self.rowCount() == 0)

    def dropEvent(self, event):  # type: ignore[override]
        # Allow reordering inside the list
        if event.source() is self:
            self.setDefaultDropAction(Qt.DropAction.MoveAction)
            super().dropEvent(event)
            self.setDefaultDropAction(Qt.DropAction.CopyAction)
            return

        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls()]
            self.external_files_dropped.emit(paths)
            event.acceptProposedAction()

    def dragEnterEvent(self, event):  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def add_row(self) -> int:
        row = self.rowCount()
        self.insertRow(row)
        self._update_placeholder()
        return row

    def clear_rows(self) -> None:
        self.setRowCount(0)
        self._update_placeholder()

    def remove_row(self, row: int) -> None:
        self.removeRow(row)
        self._update_placeholder()


class MergeWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(bool, str)

    def __init__(self, paths: List[str], output_path: str) -> None:
        super().__init__()
        self._paths = paths
        self._output_path = output_path
        self._merger = PdfMergerService()

    def run(self) -> None:
        try:
            self._merger.merge(self._paths, self._output_path, self._emit_progress)
            self.finished.emit(True, "PDFs merged successfully.")
        except Exception as exc:
            self.finished.emit(False, f"Merge failed: {exc}")

    def _emit_progress(self, current: int, total: int, path: str) -> None:
        self.progress.emit(current, total, os.path.basename(path))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Merge Pro")
        self.resize(900, 650)

        self._settings = QSettings("PDFMergePro", "PDF Merge Pro")
        self._dark_mode = True

        self._items: dict[str, PdfItem] = {}
        self._thread: QThread | None = None
        self._worker: MergeWorker | None = None
        self._progress_dialog: QProgressDialog | None = None

        self._build_ui()
        self._apply_theme()

    def _build_ui(self) -> None:
        central = QWidget()
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        self.list_widget = FileListWidget()
        self.list_widget.external_files_dropped.connect(self._handle_files_dropped)

        button_layout = QHBoxLayout()
        self.add_button = QPushButton("Add Files")
        self.remove_button = QPushButton("Remove Selected")
        self.clear_button = QPushButton("Clear List")
        self.merge_button = QPushButton("Merge PDFs")

        self.add_button.clicked.connect(self._add_files_dialog)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button.clicked.connect(self._clear_list)
        self.merge_button.clicked.connect(self._merge_pdfs)

        button_layout.addWidget(self.add_button)
        button_layout.addWidget(self.remove_button)
        button_layout.addWidget(self.clear_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self.merge_button)

        main_layout.addWidget(self.list_widget, 1)
        main_layout.addLayout(button_layout)

        central.setLayout(main_layout)
        self.setCentralWidget(central)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.menuBar().setVisible(False)

    def _add_files_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select PDF files",
            "",
            "PDF Files (*.pdf)",
        )
        if paths:
            self._handle_files_dropped(paths)

    def _handle_files_dropped(self, paths: List[str]) -> None:
        # Normalize and dedupe incoming files
        unique_paths = unique_pdf_paths(paths)
        if not unique_paths:
            self._show_status("No valid PDF files found.")
            return

        added_count = 0
        for path in unique_paths:
            norm = normalize_path(path)
            if norm in self._items:
                continue
            if not is_pdf_file(path):
                continue

            ok, pages, error = get_pdf_page_count(path)
            if not ok:
                self._show_message("PDF Error", f"Skipped {os.path.basename(path)}: {error}", QMessageBox.Icon.Warning)
                continue

            size_bytes = get_file_size_bytes(path)
            modified_ts = get_file_modified_timestamp(path)
            item = PdfItem(path=path, pages=pages, size_bytes=size_bytes, modified_ts=modified_ts)
            self._items[norm] = item
            self._add_list_item(item)
            added_count += 1

        if added_count:
            self._show_status(f"Added {added_count} PDF file(s).")

    def _add_list_item(self, item: PdfItem) -> None:
        row = self.list_widget.add_row()

        name_item = QTableWidgetItem(os.path.basename(item.path))
        name_item.setData(Qt.ItemDataRole.UserRole, item.path)

        size_item = QTableWidgetItem(format_bytes(item.size_bytes))
        pages_item = QTableWidgetItem(str(item.pages))
        modified_item = QTableWidgetItem(format_modified(item.modified_ts))

        self.list_widget.setItem(row, 0, name_item)
        self.list_widget.setItem(row, 1, size_item)
        self.list_widget.setItem(row, 2, pages_item)
        self.list_widget.setItem(row, 3, modified_item)

    def _remove_selected(self) -> None:
        selection = self.list_widget.selectionModel().selectedRows()
        if not selection:
            self._show_status("No items selected.")
            return
        rows = sorted([index.row() for index in selection], reverse=True)
        for row in rows:
            name_item = self.list_widget.item(row, 0)
            path = name_item.data(Qt.ItemDataRole.UserRole) if name_item else None
            if path:
                self._items.pop(normalize_path(path), None)
            self.list_widget.remove_row(row)
        self._show_status("Removed selected items.")

    def _clear_list(self) -> None:
        self.list_widget.clear_rows()
        self._items.clear()
        self._show_status("List cleared.")

    def _merge_pdfs(self) -> None:
        paths = self._ordered_paths()
        if not paths:
            self._show_status("Add PDFs before merging.")
            return

        last_dir = self._settings.value("output/last_dir", "", type=str)
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save merged PDF",
            last_dir or "merged.pdf",
            "PDF Files (*.pdf)",
        )
        if not output_path:
            return

        self._settings.setValue("output/last_dir", os.path.dirname(output_path))

        self._progress_dialog = QProgressDialog("Merging PDFs...", "Cancel", 0, len(paths), self)
        self._progress_dialog.setWindowTitle("Merging")
        self._progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._progress_dialog.setCancelButton(None)
        self._progress_dialog.setValue(0)
        self._progress_dialog.show()

        self._thread = QThread()
        self._worker = MergeWorker(paths, output_path)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._update_progress)
        self._worker.finished.connect(self._merge_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _update_progress(self, current: int, total: int, filename: str) -> None:
        if self._progress_dialog:
            self._progress_dialog.setMaximum(total)
            self._progress_dialog.setValue(current)
            self._progress_dialog.setLabelText(f"Merging {filename} ({current}/{total})...")

    def _merge_finished(self, ok: bool, message: str) -> None:
        if self._progress_dialog:
            self._progress_dialog.close()
        if ok:
            self._show_message("Success", message, QMessageBox.Icon.Information)
            self._show_status(message)
        else:
            self._show_message("Error", message, QMessageBox.Icon.Critical)
            self._show_status(message)

    def _ordered_paths(self) -> List[str]:
        paths = []
        for row in range(self.list_widget.rowCount()):
            item = self.list_widget.item(row, 0)
            path = item.data(Qt.ItemDataRole.UserRole) if item else None
            if path:
                paths.append(path)
        return paths

    def _apply_theme(self) -> None:
        if self._dark_mode:
            self.setStyleSheet(
                """
                QMainWindow { background: #1e1f24; color: #e6e6e6; }
                QLabel { color: #e6e6e6; }
                QTableWidget#DropArea { border: 2px dashed #3b82f6; background: #252733; border-radius: 10px; }
                QLabel#DropLabel { font-size: 18px; color: #cbd5e1; }
                QTableWidget { background: #1b1c22; border: 1px solid #30333d; color: #e6e6e6; }
                QHeaderView::section { background: #242630; color: #e6e6e6; border: 0px; padding: 6px; }
                QPushButton { background: #2f3240; color: #e6e6e6; border: 1px solid #3b3f4f; padding: 8px 14px; border-radius: 6px; }
                QPushButton:hover { background: #3a3e52; }
                QMenuBar { background: #1b1c22; color: #e6e6e6; }
                QMenuBar::item:selected { background: #2b2e3a; }
                QMenu { background: #1b1c22; color: #e6e6e6; }
                QStatusBar { background: #1b1c22; color: #cbd5e1; }
                """
            )
        else:
            self.setStyleSheet(
                """
                QMainWindow { background: #f7f7fb; color: #1b1c22; }
                QTableWidget#DropArea { border: 2px dashed #2563eb; background: #ffffff; border-radius: 10px; }
                QLabel#DropLabel { font-size: 18px; color: #334155; }
                QTableWidget { background: #ffffff; border: 1px solid #e2e8f0; color: #1b1c22; }
                QHeaderView::section { background: #eef2f7; color: #1b1c22; border: 0px; padding: 6px; }
                QPushButton { background: #ffffff; color: #1b1c22; border: 1px solid #d0d7e2; padding: 8px 14px; border-radius: 6px; }
                QPushButton:hover { background: #f1f5f9; }
                QMenuBar { background: #ffffff; color: #1b1c22; }
                QMenuBar::item:selected { background: #e2e8f0; }
                QMenu { background: #ffffff; color: #1b1c22; }
                QStatusBar { background: #ffffff; color: #475569; }
                """
            )

    def _show_status(self, message: str) -> None:
        self.statusBar().showMessage(message, 5000)

    def _show_message(self, title: str, message: str, icon: QMessageBox.Icon) -> None:
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(message)
        box.exec()
