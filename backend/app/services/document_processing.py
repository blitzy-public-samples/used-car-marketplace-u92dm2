import io
import re
from PyPDF2 import PdfReader
from typing import Dict, Any


def _extract_text(document_data: bytes, document_type: str = None) -> str:
    # Extract text from a PDF stream or decode raw bytes as UTF-8.
    is_pdf = document_type == 'pdf' or document_data[:5] == b'%PDF-'
    if is_pdf:
        try:
            reader = PdfReader(io.BytesIO(document_data))
            return "".join((page.extract_text() or "") for page in reader.pages)
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
