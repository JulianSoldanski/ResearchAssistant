import fitz  # PyMuPDF

MAX_CHARS = 100_000


def extract_text(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    pages = [page.get_text() for page in doc]
    doc.close()
    text = "\n".join(pages)
    return text[:MAX_CHARS]
