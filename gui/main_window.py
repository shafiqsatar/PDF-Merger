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
from PyQt6.QtGui import QCursor, QDrag, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QToolButton,
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
from utils.resource_utils import resource_path


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
        self.setColumnCount(7)
        self.setHorizontalHeaderLabels(
            ["#", "Name", "Size", "Pages", "Page ranges", "Selected pages", "Modified"]
        )
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setDropIndicatorShown(False)
        self.setDragDropOverwriteMode(False)
        self.setAlternatingRowColors(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setMouseTracking(True)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(0, self.horizontalHeader().ResizeMode.Fixed)
        self.horizontalHeader().setSectionResizeMode(1, self.horizontalHeader().ResizeMode.Interactive)
        self.horizontalHeader().setSectionResizeMode(2, self.horizontalHeader().ResizeMode.Interactive)
        self.horizontalHeader().setSectionResizeMode(3, self.horizontalHeader().ResizeMode.Interactive)
        self.horizontalHeader().setSectionResizeMode(4, self.horizontalHeader().ResizeMode.Interactive)
        self.horizontalHeader().setSectionResizeMode(5, self.horizontalHeader().ResizeMode.Interactive)
        self.horizontalHeader().setSectionResizeMode(6, self.horizontalHeader().ResizeMode.Interactive)
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft)
        self.setSortingEnabled(True)
        self.horizontalHeader().setSortIndicatorShown(True)
        self.horizontalHeader().sectionClicked.connect(self._handle_header_sort)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(True)
        self.setColumnWidth(0, 36)
        self._last_sort_section: int | None = None
        self._sort_orders: dict[int, Qt.SortOrder] = {}

        self._placeholder = QLabel("Drag and drop PDF files here", self.viewport())
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setObjectName("DropLabel")
        self._placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._drop_row: int | None = None
        self._drop_pos_y: int | None = None
        self._indicator_opacity = 0.0
        self._indicator_anim = QPropertyAnimation(self, b"indicatorOpacity", self)
        self._indicator_anim.setDuration(140)
        self._drag_rows: list[int] = []
        self._hover_row: int | None = None
        self._update_placeholder()

    def resizeEvent(self, event):  # type: ignore[override]
        super().resizeEvent(event)
        self._center_placeholder()

    def _center_placeholder(self) -> None:
        if not self._placeholder:
            return
        size = self.viewport().size()
        box_width = min(520, max(360, int(size.width() * 0.6)))
        box_height = 90
        x = max(0, (size.width() - box_width) // 2)
        y = max(0, (size.height() - box_height) // 2)
        self._placeholder.setGeometry(x, y, box_width, box_height)

    def _update_placeholder(self) -> None:
        self._placeholder.setVisible(self.rowCount() == 0)
        self._center_placeholder()

    def dropEvent(self, event):  # type: ignore[override]
        # Allow reordering inside the list
        if event.source() is self:
            event.setDropAction(Qt.DropAction.CopyAction)
            pos = self._to_viewport_pos(event.position().toPoint())
            self._move_rows_to_target(self._drag_rows, self._drop_target_row(pos))
            self._drag_rows = []
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
        if self.isSortingEnabled():
            self.setSortingEnabled(False)
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
        self._clear_drop_row()

    def paintEvent(self, event):  # type: ignore[override]
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        if self._hover_row is not None:
            if not self.selectionModel().isRowSelected(
                self._hover_row, self.model().index(self._hover_row, 0)
            ):
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
        if self._last_sort_section == section:
            current_order = self._sort_orders.get(section, Qt.SortOrder.AscendingOrder)
            new_order = (
                Qt.SortOrder.DescendingOrder
                if current_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            new_order = Qt.SortOrder.AscendingOrder
        self._last_sort_section = section
        self._sort_orders[section] = new_order
        self.horizontalHeader().setSortIndicator(section, new_order)
        self.sortItems(section, new_order)

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
        self.setWindowTitle("PDF Merger")
        icon_ico = resource_path(os.path.join("gui", "assets", "app_icon.ico"))
        if os.path.exists(icon_ico):
            self.setWindowIcon(QIcon(icon_ico))
        self.resize(900, 650)
        self.setMinimumSize(720, 520)

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
        self.list_widget.selectionModel().selectionChanged.connect(self._update_action_state)

        toolbar_layout = QHBoxLayout()
        self.add_button = QToolButton()
        self.add_button.setText("Add")
        self.add_button.setObjectName("ToolButton")

        self.clear_button = QToolButton()
        self.clear_button.setText("Clear")
        self.clear_button.setObjectName("ToolButton")

        self.remove_button = QToolButton()
        self.remove_button.setText("Remove")
        self.remove_button.setObjectName("ToolButtonDisabled")
        self.remove_button.setEnabled(False)

        self.move_up_button = QToolButton()
        self.move_up_button.setText("Move Up")
        self.move_up_button.setObjectName("ToolButtonDisabled")
        self.move_up_button.setEnabled(False)

        self.move_down_button = QToolButton()
        self.move_down_button.setText("Move Down")
        self.move_down_button.setObjectName("ToolButtonDisabled")
        self.move_down_button.setEnabled(False)

        toolbar_layout.addWidget(self.add_button)
        toolbar_layout.addWidget(self.clear_button)
        toolbar_layout.addWidget(self.remove_button)
        toolbar_layout.addWidget(self.move_up_button)
        toolbar_layout.addWidget(self.move_down_button)
        toolbar_layout.addStretch(1)

        self.add_button.clicked.connect(self._add_files_dialog)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button.clicked.connect(self._clear_list)
        self.move_up_button.clicked.connect(lambda: self._move_selection(-1))
        self.move_down_button.clicked.connect(lambda: self._move_selection(1))

        self.total_pages_label = QLabel("Total pages: 0")
        self.total_pages_label.setObjectName("TotalPagesLabel")

        dest_layout = QHBoxLayout()
        dest_label = QLabel("Destination folder:")
        self.dest_input = QLineEdit()
        self.dest_input.setPlaceholderText("Select a folder...")
        self.dest_browse_button = QToolButton()
        self.dest_browse_button.setText("Browse")
        self.dest_browse_button.setObjectName("ToolButton")
        self.dest_browse_button.clicked.connect(self._browse_destination)
        dest_layout.addWidget(dest_label)
        dest_layout.addWidget(self.dest_input, 1)
        dest_layout.addWidget(self.dest_browse_button)

        name_layout = QHBoxLayout()
        name_label = QLabel("File name:")
        self.filename_input = QLineEdit("Merge")
        self.filename_input.setPlaceholderText("Merge")
        name_layout.addWidget(name_label)
        name_layout.addWidget(self.filename_input, 1)

        self.overwrite_checkbox = QCheckBox("Overwrite if file already exists")

        self.merge_button = QPushButton("Merge PDFs")
        self.merge_button.setObjectName("MergeButton")
        self.merge_button.clicked.connect(self._merge_pdfs)

        main_layout.addLayout(toolbar_layout)
        main_layout.addWidget(self.list_widget, 1)
        main_layout.addWidget(self.total_pages_label)
        main_layout.addLayout(dest_layout)
        main_layout.addLayout(name_layout)
        main_layout.addWidget(self.overwrite_checkbox)
        main_layout.addWidget(self.merge_button)

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

    def _browse_destination(self) -> None:
        last_dir = self._settings.value("output/last_dir", "", type=str)
        folder = QFileDialog.getExistingDirectory(self, "Select destination folder", last_dir or "")
        if folder:
            self.dest_input.setText(folder)
            self._settings.setValue("output/last_dir", folder)

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
        self._reindex_rows()
        self._update_action_state()

    def _add_list_item(self, item: PdfItem) -> None:
        row = self.list_widget.add_row()

        index_item = SortableItem()
        index_item.setData(Qt.ItemDataRole.DisplayRole, str(row + 1))
        index_item.setData(Qt.ItemDataRole.UserRole, row + 1)

        name_item = QTableWidgetItem(os.path.basename(item.path))
        name_item.setData(Qt.ItemDataRole.UserRole, item.path)

        size_item = SortableItem()
        size_item.setData(Qt.ItemDataRole.DisplayRole, format_bytes(item.size_bytes))
        size_item.setData(Qt.ItemDataRole.UserRole, item.size_bytes)

        pages_item = SortableItem()
        pages_item.setData(Qt.ItemDataRole.DisplayRole, str(item.pages))
        pages_item.setData(Qt.ItemDataRole.UserRole, item.pages)

        ranges_item = QTableWidgetItem("")
        selected_pages_item = QTableWidgetItem("")

        modified_item = SortableItem()
        modified_item.setData(Qt.ItemDataRole.DisplayRole, format_modified(item.modified_ts))
        modified_item.setData(Qt.ItemDataRole.UserRole, item.modified_ts)

        self.list_widget.setItem(row, 0, index_item)
        self.list_widget.setItem(row, 1, name_item)
        self.list_widget.setItem(row, 2, size_item)
        self.list_widget.setItem(row, 3, pages_item)
        self.list_widget.setItem(row, 4, ranges_item)
        self.list_widget.setItem(row, 5, selected_pages_item)
        self.list_widget.setItem(row, 6, modified_item)

        self._update_total_pages()

    def _remove_selected(self) -> None:
        selection = self.list_widget.selectionModel().selectedRows()
        if not selection:
            self._show_status("No items selected.")
            return
        rows = sorted([index.row() for index in selection], reverse=True)
        for row in rows:
            name_item = self.list_widget.item(row, 1)
            path = name_item.data(Qt.ItemDataRole.UserRole) if name_item else None
            if path:
                self._items.pop(normalize_path(path), None)
            self.list_widget.remove_row(row)
        self._reindex_rows()
        self._update_total_pages()
        self._update_action_state()
        self._show_status("Removed selected items.")

    def _clear_list(self) -> None:
        self.list_widget.clear_rows()
        self._items.clear()
        self._update_total_pages()
        self._update_action_state()
        self._show_status("List cleared.")

    def _merge_pdfs(self) -> None:
        paths = self._ordered_paths()
        if not paths:
            self._show_status("Add PDFs before merging.")
            return

        destination = self.dest_input.text().strip()
        if not destination:
            self._show_status("Select a destination folder.")
            return

        filename = self.filename_input.text().strip() or "Merge"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        output_path = os.path.join(destination, filename)

        if os.path.exists(output_path) and not self.overwrite_checkbox.isChecked():
            self._show_message(
                "File Exists",
                "A file with this name already exists. Enable overwrite to replace it.",
                QMessageBox.Icon.Warning,
            )
            return

        self._settings.setValue("output/last_dir", destination)

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
            item = self.list_widget.item(row, 1)
            path = item.data(Qt.ItemDataRole.UserRole) if item else None
            if path:
                paths.append(path)
        return paths

    def _set_tool_button_state(self, button: QToolButton, enabled: bool) -> None:
        button.setEnabled(enabled)
        button.setObjectName("ToolButton" if enabled else "ToolButtonDisabled")
        button.style().unpolish(button)
        button.style().polish(button)

    def _update_action_state(self) -> None:
        selection = self.list_widget.selectionModel().selectedRows()
        has_selection = bool(selection)
        if has_selection:
            rows = [index.row() for index in selection]
            min_row = min(rows)
            max_row = max(rows)
        else:
            min_row = 0
            max_row = -1

        can_move_up = has_selection and min_row > 0
        can_move_down = has_selection and max_row < self.list_widget.rowCount() - 1

        self._set_tool_button_state(self.remove_button, has_selection)
        self._set_tool_button_state(self.move_up_button, can_move_up)
        self._set_tool_button_state(self.move_down_button, can_move_down)

    def _move_selection(self, direction: int) -> None:
        selection = self.list_widget.selectionModel().selectedRows()
        if not selection:
            return
        rows = sorted({index.row() for index in selection})
        if direction < 0:
            min_row = rows[0]
            if min_row <= 0:
                return
            target_row = min_row - 1
        else:
            max_row = rows[-1]
            if max_row >= self.list_widget.rowCount() - 1:
                return
            target_row = max_row + 2
        self.list_widget._move_rows_to_target(rows, target_row)
        self._reindex_rows()
        self._update_action_state()

    def _reindex_rows(self) -> None:
        for row in range(self.list_widget.rowCount()):
            index_item = self.list_widget.item(row, 0)
            if index_item:
                index_item.setData(Qt.ItemDataRole.DisplayRole, str(row + 1))
                index_item.setData(Qt.ItemDataRole.UserRole, row + 1)

    def _update_total_pages(self) -> None:
        total = sum(item.pages for item in self._items.values())
        self.total_pages_label.setText(f"Total pages: {total}")

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #1b1c22; color: #e5e7eb; }
            QLabel { color: #e5e7eb; }
            QLabel#DropLabel {
                color: #9aa0a6;
                background: #1b1c22;
                border: 2px dashed #4b5563;
                border-radius: 12px;
                font-size: 16px;
            }
            QTableWidget#DropArea {
                background: #111318;
                border: 1px solid #2f333d;
                border-radius: 6px;
            }
            QTableWidget {
                background: #111318;
                border: 1px solid #2f333d;
                color: #e5e7eb;
                gridline-color: #2a2f3a;
                outline: none;
            }
            QTableWidget::item { border: none; }
            QTableWidget::item:hover { background: transparent; }
            QTableWidget::item:focus { outline: none; }
            QTableWidget::item:selected { outline: none; }
            QTableWidget::item:selected:active { outline: none; }
            QTableWidget::item:selected:focus { outline: none; }
            QTableWidget::item:selected { background: #1e3a8a; color: #e5e7eb; }
            QHeaderView::section {
                background: #20242c;
                color: #e5e7eb;
                border: 1px solid #2f333d;
                padding: 4px 6px;
                font-weight: 600;
            }
            QToolButton#ToolButton {
                background: #242834;
                color: #e5e7eb;
                border: 1px solid #2f333d;
                padding: 4px 12px;
                border-radius: 8px;
            }
            QToolButton#ToolButton:hover { background: #2c3140; }
            QToolButton#ToolButtonDisabled {
                background: #242834;
                color: #8b93a1;
                border: 1px solid #2f333d;
                padding: 4px 12px;
                border-radius: 8px;
            }
            QLineEdit {
                background: #111318;
                color: #e5e7eb;
                border: 1px solid #2f333d;
                padding: 4px 8px;
                border-radius: 6px;
            }
            QCheckBox {
                color: #e5e7eb;
                spacing: 8px;
            }
            QLabel#TotalPagesLabel {
                color: #e5e7eb;
                background: #20242c;
                border: 1px solid #2f333d;
                border-radius: 8px;
                padding: 4px 10px;
            }
            QPushButton#MergeButton {
                background: #ef4444;
                border: 1px solid #dc2626;
                color: #ffffff;
                padding: 8px 14px;
                border-radius: 8px;
            }
            QPushButton#MergeButton:hover { background: #f05252; }
            QStatusBar { background: #1b1c22; color: #9aa0a6; }
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
