import hashlib


def make_hash(pdf_text: str, prompt: str, model: str = "") -> str:
    content = pdf_text + "\n---PROMPT---\n" + prompt
    if model:
        content += "\n---MODEL---\n" + model
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
