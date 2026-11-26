"""Contact redaction shared by project context and on-device intake."""
import re


def redact_source_contacts(text: str) -> str:
    """Remove obvious contact and identity numbers before model context."""
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "【邮箱已隐藏】", text)
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "【电话已隐藏】", text)
    return re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", "【证件号已隐藏】", text)
