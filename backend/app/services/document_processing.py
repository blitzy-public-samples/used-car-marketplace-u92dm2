import io
import re
import signal
import threading
from PyPDF2 import PdfReader
from typing import Dict, Any

# Ceilings applied to each processed document: maximum bytes accepted, maximum
# PDF pages parsed, and maximum seconds spent on PDF text extraction.
_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_PDF_PAGES = 100
_PDF_EXTRACTION_TIMEOUT_SECONDS = 10


class _PdfExtractionTimeout(Exception):
    # Raised when PDF text extraction exceeds _PDF_EXTRACTION_TIMEOUT_SECONDS.
    pass


def _raise_pdf_extraction_timeout(signum, frame):
    # SIGALRM handler that stops a long-running PDF text extraction.
    raise _PdfExtractionTimeout()


def _read_pdf_text(document_data: bytes) -> str:
    # Read text from at most _MAX_PDF_PAGES pages of a PDF stream.
    reader = PdfReader(io.BytesIO(document_data))
    pages = reader.pages[:_MAX_PDF_PAGES]
    return "".join((page.extract_text() or "") for page in pages)


def _extract_pdf_text_guarded(document_data: bytes) -> str:
    # Read PDF text under a wall-clock ceiling when SIGALRM is available on the
    # main thread; otherwise read it directly.
    supports_alarm = (
        hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    )
    if not supports_alarm:
        return _read_pdf_text(document_data)
    previous_handler = signal.signal(signal.SIGALRM, _raise_pdf_extraction_timeout)
    signal.alarm(_PDF_EXTRACTION_TIMEOUT_SECONDS)
    try:
        return _read_pdf_text(document_data)
    finally:
        signal.alarm(0)
        signal.signal(
            signal.SIGALRM,
            previous_handler if previous_handler is not None else signal.SIG_DFL,
        )


def _extract_text(document_data: bytes, document_type: str = None) -> str:
    # Extract text from a PDF stream or decode raw bytes as UTF-8.
    if not document_data or len(document_data) > _MAX_DOCUMENT_BYTES:
        return ""
    is_pdf = document_type == 'pdf' or document_data[:5] == b'%PDF-'
    if is_pdf:
        try:
            return _extract_pdf_text_guarded(document_data)
        except Exception:
            pass
    try:
        return document_data.decode('utf-8', errors='ignore')
    except Exception:
        return ""


def classify_document(document_data: bytes) -> str:
    # Return one of: receipt, summary, service.
    text = _extract_text(document_data).lower()
    if any(k in text for k in ('receipt', 'invoice', 'amount paid', 'total due')):
        return "receipt"
    if any(k in text for k in ('summary', 'overview', 'history report')):
        return "summary"
    return "service"


def process_maintenance_document(document_data: bytes, document_type: str) -> Dict[str, Any]:
    text = _extract_text(document_data, document_type)
    info: Dict[str, Any] = {"category": classify_document(document_data),
                            "repair_type": None, "cost": None, "date": None,
                            "text_excerpt": text[:500]}
    cost = re.search(r'\$\s?(\d[\d,]*\.?\d{0,2})', text)
    if cost:
        info["cost"] = float(cost.group(1).replace(',', ''))
    date = re.search(r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b', text)
    if date:
        info["date"] = date.group(1)
    for kw in ('oil change', 'brake', 'tire', 'transmission', 'engine', 'battery', 'inspection'):
        if kw in text.lower():
            info["repair_type"] = kw
            break
    return info
