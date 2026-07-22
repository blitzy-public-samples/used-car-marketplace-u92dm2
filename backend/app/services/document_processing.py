import io
import re
import math
import signal
import threading
from datetime import datetime
from PyPDF2 import PdfReader
from typing import Dict, Any, Optional

# Ceilings applied to each processed document: maximum bytes accepted, maximum
# PDF pages parsed, and maximum seconds spent on PDF text extraction.
_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_PDF_PAGES = 100
_PDF_EXTRACTION_TIMEOUT_SECONDS = 10

# Keyword sets used to classify already-extracted document text.
_RECEIPT_KEYWORDS = ('receipt', 'invoice', 'amount paid', 'total due')
_SUMMARY_KEYWORDS = ('summary', 'overview', 'history report')
_REPAIR_KEYWORDS = ('oil change', 'brake', 'tire', 'transmission',
                    'engine', 'battery', 'inspection')

# Bounds applied to extracted business values: the maximum number of cost
# digits parsed, the accepted cost range, and the recognised calendar formats.
_MAX_COST_DIGITS = 12
_MAX_COST_VALUE = 1_000_000_000.0
_COST_PATTERN = re.compile(
    r'\$\s?(\d{1,' + str(_MAX_COST_DIGITS) + r'}(?:,\d{3})*(?:\.\d{1,2})?)'
)
_DATE_PATTERN = re.compile(
    r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})\b'
)
_DATE_FORMATS = ('%m/%d/%Y', '%m/%d/%y', '%m-%d-%Y', '%m-%d-%y', '%Y-%m-%d')


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
    previous_handler = signal.signal(
        signal.SIGALRM, _raise_pdf_extraction_timeout
    )
    signal.alarm(_PDF_EXTRACTION_TIMEOUT_SECONDS)
    try:
        return _read_pdf_text(document_data)
    finally:
        signal.alarm(0)
        signal.signal(
            signal.SIGALRM,
            previous_handler if previous_handler is not None
            else signal.SIG_DFL,
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


def _classify_text(text: str) -> str:
    # Return one of receipt, summary, service for already-extracted text.
    lowered = text.lower()
    if any(keyword in lowered for keyword in _RECEIPT_KEYWORDS):
        return "receipt"
    if any(keyword in lowered for keyword in _SUMMARY_KEYWORDS):
        return "summary"
    return "service"


def _parse_cost(text: str) -> Optional[float]:
    # Return a finite, in-range cost from the first currency match, else None.
    match = _COST_PATTERN.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(',', ''))
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0 or value > _MAX_COST_VALUE:
        return None
    return value


def _parse_date(text: str) -> Optional[str]:
    # Return the first token that is a valid calendar date, else None.
    match = _DATE_PATTERN.search(text)
    if not match:
        return None
    token = match.group(1)
    for date_format in _DATE_FORMATS:
        try:
            datetime.strptime(token, date_format)
            return token
        except ValueError:
            continue
    return None


def classify_document(document_data: bytes) -> str:
    # Return one of: receipt, summary, service.
    return _classify_text(_extract_text(document_data))


def process_maintenance_document(document_data: bytes, document_type: str) -> Dict[str, Any]:
    # Normalize bytearray to bytes and reject empty or non-bytes content.
    if isinstance(document_data, bytearray):
        document_data = bytes(document_data)
    if not isinstance(document_data, bytes) or not document_data:
        raise ValueError("document content must be non-empty bytes")
    text = _extract_text(document_data, document_type)
    info: Dict[str, Any] = {"category": _classify_text(text),
                            "repair_type": None, "cost": None, "date": None,
                            "text_excerpt": text[:500]}
    info["cost"] = _parse_cost(text)
    info["date"] = _parse_date(text)
    lowered = text.lower()
    for keyword in _REPAIR_KEYWORDS:
        if keyword in lowered:
            info["repair_type"] = keyword
            break
    return info
