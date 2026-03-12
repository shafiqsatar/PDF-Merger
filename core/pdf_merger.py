from typing import Callable, Iterable, Optional

from pypdf import PdfReader, PdfWriter


ProgressCallback = Callable[[int, int, str], None]


class PdfMergerService:
    def merge(
        self,
        input_paths: Iterable[str],
        output_path: str,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        """
        Merge PDFs using pypdf.PdfWriter.
        progress_callback(current_index, total_count, filename)
        """
        paths = list(input_paths)
        total = len(paths)
        writer = PdfWriter()
        for index, path in enumerate(paths, start=1):
            if progress_callback:
                progress_callback(index, total, path)
            reader = PdfReader(path)
            for page in reader.pages:
                writer.add_page(page)

        with open(output_path, "wb") as output_handle:
            writer.write(output_handle)
