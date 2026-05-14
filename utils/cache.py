import hashlib


def make_hash(pdf_text: str, prompt: str) -> str:
    content = pdf_text + "\n---PROMPT---\n" + prompt
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
