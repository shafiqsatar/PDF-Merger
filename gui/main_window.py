import os
from dataclasses import dataclass
from typing import List

from PyQt6.QtCore import (
    Qt,
    QThread,
    pyqtSignal,
    QObject,
    QSettings,
    pyqtProperty,
    QPropertyAnimation,
    QRect,
)
from PyQt6.QtGui import QCursor, QDrag, QPainter, QPalette, QPixmap
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


class SortableItem(QTableWidgetItem):
    def __lt__(self, other: "QTableWidgetItem") -> bool:  # type: ignore[override]
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if left is not None and right is not None:
            return left < right
        return super().__lt__(other)


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
        self.setDropIndicatorShown(False)
        self.setDragDropOverwriteMode(False)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setMouseTracking(True)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, self.horizontalHeader().ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, self.horizontalHeader().ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, self.horizontalHeader().ResizeMode.ResizeToContents)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
        self.setSortingEnabled(True)
        self.horizontalHeader().sectionClicked.connect(self._handle_header_sort)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)

        self._placeholder = QLabel("Drag and drop PDF files here", self)
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setObjectName("DropLabel")
        self._placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._drop_row: int | None = None
        self._drop_pos_y: int | None = None
        self._indicator_opacity = 0.0
        self._indicator_anim = QPropertyAnimation(self, b"indicatorOpacity", self)
        self._indicator_anim.setDuration(140)
        self._drag_rows: list[int] = []
        self._drag_active = False
        self._sorting_disabled_by_drag = False
        self._hover_row: int | None = None
        self._update_placeholder()

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        self._placeholder.resize(self.viewport().size())

    def _update_placeholder(self) -> None:
        self._placeholder.setVisible(self.rowCount() == 0)

    def dropEvent(self, event):  # type: ignore[override]
        # Allow reordering inside the list
        if event.source() is self:
            event.setDropAction(Qt.DropAction.CopyAction)
            pos = self._to_viewport_pos(event.position().toPoint())
            self._move_rows_to_target(self._drag_rows, self._drop_target_row(pos))
            self._drag_rows = []
            self._drag_active = False
            event.acceptProposedAction()
            self._clear_drop_row()
            return

        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            paths = [url.toLocalFile() for url in event.mimeData().urls()]
            self.external_files_dropped.emit(paths)
            event.acceptProposedAction()
        self._clear_drop_row()

    def dragEnterEvent(self, event):  # type: ignore[override]
        if event.source() is self:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
        elif event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # type: ignore[override]
        if event.source() is self:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            pos = self._to_viewport_pos(event.position().toPoint())
            self._update_drop_row(pos)
        elif event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            pos = self._to_viewport_pos(event.position().toPoint())
            self._update_drop_row(pos)
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):  # type: ignore[override]
        self._clear_drop_row()
        super().dragLeaveEvent(event)

    def leaveEvent(self, event):  # type: ignore[override]
        self._hover_row = None
        self.viewport().update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):  # type: ignore[override]
        index = self.indexAt(event.position().toPoint())
        row = index.row() if index.isValid() else None
        if self._hover_row != row:
            self._hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def startDrag(self, supportedActions) -> None:  # type: ignore[override]
        self._drag_rows = sorted({idx.row() for idx in self.selectionModel().selectedRows()})
        self._drag_active = True
        if self.isSortingEnabled():
            self.setSortingEnabled(False)
            self._sorting_disabled_by_drag = True
        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return

        drag = QDrag(self)
        mime_data = self.model().mimeData(indexes)
        drag.setMimeData(mime_data)

        region = self.visualRegionForSelection(self.selectionModel().selection())
        rect = region.boundingRect()
        if not rect.isNull():
            pixmap = self.viewport().grab(rect)
            if not pixmap.isNull():
                translucent = QPixmap(pixmap.size())
                translucent.fill(Qt.GlobalColor.transparent)
                painter = QPainter(translucent)
                painter.setOpacity(0.5)
                painter.drawPixmap(0, 0, pixmap)
                painter.end()
                drag.setPixmap(translucent)
                cursor_pos = self.viewport().mapFromGlobal(QCursor.pos())
                drag.setHotSpot(cursor_pos - rect.topLeft())

        drag.exec(supportedActions)
        self._drag_active = False
        self._clear_drop_row()

    def paintEvent(self, event):  # type: ignore[override]
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        if self._hover_row is not None:
            hover_index = self.model().index(self._hover_row, 0)
            hover_rect = self.visualRect(hover_index)
            hover_rect.setLeft(0)
            hover_rect.setRight(self.viewport().width())
            hover_color = self.palette().color(QPalette.ColorRole.Highlight)
            hover_color.setAlpha(40)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(hover_color)
            painter.drawRect(hover_rect)

        if self._drop_row is None or self._drop_pos_y is None or self._indicator_opacity <= 0:
            return
        color = self.palette().color(QPalette.ColorRole.Highlight)
        color.setAlphaF(min(0.6, max(0.0, self._indicator_opacity)))
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        left = 4
        right = self.viewport().width() - 4
        y = self._drop_pos_y
        height = 8
        rect = QRect(left, y - height // 2, right - left, height)
        painter.drawRoundedRect(rect, 4, 4)

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

    def _set_drop_row(self, row: int | None) -> None:
        if self._drop_row == row:
            return
        self._drop_row = row
        self._fade_indicator(1.0)
        self.viewport().update()

    def _clear_drop_row(self) -> None:
        if self._drop_row is None:
            return
        self._fade_indicator(0.0)

    def _update_drop_row(self, pos) -> None:
        if self.rowCount() == 0:
            self._drop_pos_y = self.viewport().rect().center().y()
            self._set_drop_row(0)
            return
        row = self._drop_target_row(pos)
        self._drop_pos_y = self._drop_indicator_y(row)
        self._set_drop_row(row)

    def _drop_target_row(self, pos) -> int:
        index = self.indexAt(pos)
        if index.isValid():
            rect = self.visualRect(index)
            return index.row() + (1 if pos.y() >= rect.center().y() else 0)
        return self.rowCount()

    def _drop_indicator_y(self, target_row: int) -> int:
        if self.rowCount() == 0:
            return self.viewport().rect().center().y()
        if target_row <= 0:
            first_rect = self.visualRect(self.model().index(0, 0))
            return first_rect.top()
        if target_row >= self.rowCount():
            last_rect = self.visualRect(self.model().index(self.rowCount() - 1, 0))
            return last_rect.bottom()
        rect = self.visualRect(self.model().index(target_row, 0))
        return rect.top()

    def _move_rows_to_target(self, rows: list[int], target_row: int, *, preview: bool = False) -> None:
        if not rows:
            return

        min_row = rows[0]
        max_row = rows[-1]
        if min_row <= target_row <= max_row + 1:
            return

        row_data: list[list[QTableWidgetItem | None]] = []
        for row in rows:
            items: list[QTableWidgetItem | None] = []
            for col in range(self.columnCount()):
                item = self.item(row, col)
                items.append(item.clone() if item else None)
            row_data.append(items)

        self.setUpdatesEnabled(False)
        was_sorting = self.isSortingEnabled()
        if was_sorting:
            self.setSortingEnabled(False)
        try:
            for row in reversed(rows):
                self.removeRow(row)

            offset = sum(1 for row in rows if row < target_row)
            target_row -= offset
            target_row = max(0, min(target_row, self.rowCount()))

            insert_at = target_row
            for items in row_data:
                self.insertRow(insert_at)
                for col, item in enumerate(items):
                    self.setItem(insert_at, col, item or QTableWidgetItem(""))
                insert_at += 1

            if not preview:
                self.clearSelection()
                for row in range(target_row, target_row + len(row_data)):
                    self.selectRow(row)
                self._update_placeholder()
        finally:
            if was_sorting:
                self.setSortingEnabled(True)
            self.setUpdatesEnabled(True)

    # Preview reordering is intentionally disabled to prevent jitter.

    def _to_viewport_pos(self, pos):
        return self.viewport().mapFrom(self, pos)

    def _handle_header_sort(self, section: int) -> None:
        if not self.isSortingEnabled():
            self.setSortingEnabled(True)
        self.sortItems(section)

    def _fade_indicator(self, target: float) -> None:
        self._indicator_anim.stop()
        self._indicator_anim.setStartValue(self._indicator_opacity)
        self._indicator_anim.setEndValue(target)
        self._indicator_anim.start()

    def _get_indicator_opacity(self) -> float:
        return self._indicator_opacity

    def _set_indicator_opacity(self, value: float) -> None:
        self._indicator_opacity = value
        if value <= 0.0 and self._indicator_anim.endValue() == 0.0:
            self._drop_row = None
        self.viewport().update()

    indicatorOpacity = pyqtProperty(float, _get_indicator_opacity, _set_indicator_opacity)


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
        self.merge_button.setObjectName("MergeButton")

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

        size_item = SortableItem()
        size_item.setData(Qt.ItemDataRole.DisplayRole, format_bytes(item.size_bytes))
        size_item.setData(Qt.ItemDataRole.UserRole, item.size_bytes)

        pages_item = SortableItem()
        pages_item.setData(Qt.ItemDataRole.DisplayRole, str(item.pages))
        pages_item.setData(Qt.ItemDataRole.UserRole, item.pages)

        modified_item = SortableItem()
        modified_item.setData(Qt.ItemDataRole.DisplayRole, format_modified(item.modified_ts))
        modified_item.setData(Qt.ItemDataRole.UserRole, item.modified_ts)

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
                QPushButton:pressed { background: #262a39; border-color: #242731; padding-top: 9px; padding-bottom: 7px; }
                QPushButton#MergeButton { background: #dc2626; border-color: #b91c1c; color: #ffffff; }
                QPushButton#MergeButton:hover { background: #ef4444; }
                QPushButton#MergeButton:pressed { background: #b91c1c; border-color: #991b1b; padding-top: 9px; padding-bottom: 7px; }
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
                QPushButton:pressed { background: #e2e8f0; border-color: #cbd5e1; padding-top: 9px; padding-bottom: 7px; }
                QPushButton#MergeButton { background: #dc2626; border-color: #b91c1c; color: #ffffff; }
                QPushButton#MergeButton:hover { background: #ef4444; }
                QPushButton#MergeButton:pressed { background: #b91c1c; border-color: #991b1b; padding-top: 9px; padding-bottom: 7px; }
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
