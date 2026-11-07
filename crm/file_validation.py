"""Bounded content checks. Files remain private; optional scanner fails closed."""

import io
import os
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from django.core.exceptions import ValidationError


def validate_upload(upload):
    if upload.size > 10 * 1024 * 1024:
        raise ValidationError("Максимум 10 МБ.")
    position = upload.tell()
    try:
        upload.seek(0)
        data = upload.read(10 * 1024 * 1024 + 1)
        suffix = Path(upload.name).suffix.lower()
        if suffix in (".xlsx", ".docx"):
            with ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if (
                    len(infos) > 3000
                    or sum(i.file_size for i in infos) > 50 * 1024 * 1024
                ):
                    raise ValueError("Превышен размер распакованного документа")
                names = archive.namelist()
                required = (
                    "xl/workbook.xml" if suffix == ".xlsx" else "word/document.xml"
                )
                if required not in names or "[Content_Types].xml" not in names:
                    raise ValueError("Содержимое не соответствует расширению")
                for entry in infos:
                    name = entry.filename.lower()
                    if (
                        entry.flag_bits & 1
                        or ".." in Path(name).parts
                        or name.startswith("/")
                    ):
                        raise ValueError("Недопустимая структура архива")
                    if any(
                        s in name
                        for s in (
                            "vbaproject",
                            "embeddings/",
                            "activex/",
                            "externallinks/",
                        )
                    ):
                        raise ValueError("Активное содержимое запрещено")
                    if name.endswith((".xml", ".rels")):
                        xml = archive.read(entry)
                        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                            raise ValueError("DTD запрещён")
                        root = ElementTree.fromstring(xml)
                        if name.endswith(".rels") and any(
                            e.get("TargetMode") == "External" for e in root
                        ):
                            raise ValueError("Внешние связи запрещены")
                if archive.testzip():
                    raise ValueError("Повреждённый архив")
        elif suffix == ".pdf":
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError

            if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
                raise ValueError("Повреждённый PDF")
            if any(
                token.lower() in data.lower()
                for token in (
                    b"/JavaScript",
                    b"/JS",
                    b"/Launch",
                    b"/EmbeddedFile",
                    b"/OpenAction",
                )
            ):
                raise ValueError("Активное содержимое PDF запрещено")
            try:
                reader = PdfReader(io.BytesIO(data), strict=True)
                if reader.is_encrypted or not 0 < len(reader.pages) <= 1000:
                    raise ValueError("Зашифрованный или слишком большой PDF")
            except PdfReadError as exc:
                raise ValueError("Повреждённый PDF") from exc
        elif suffix == ".txt":
            content = data.decode("utf-8-sig")
            if (
                "\x00" in content
                or "<script" in content.lower()
                or "<html" in content.lower()
            ):
                raise ValueError("Недопустимое содержимое текста")
        elif suffix in (".png", ".jpg", ".jpeg"):
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError("Изображение слишком большое")
                if image.format != ("PNG" if suffix == ".png" else "JPEG"):
                    raise ValueError("Формат изображения не соответствует расширению")
                image.verify()
        else:
            raise ValueError("Недопустимое расширение")
        scanner = os.getenv("DOCUMENT_SCANNER")
        if scanner:
            with tempfile.TemporaryDirectory(prefix="crm-quarantine-") as folder:
                path = Path(folder) / ("upload" + suffix)
                path.write_bytes(data)
                result = subprocess.run(
                    [scanner, str(path)], capture_output=True, timeout=30
                )
                if result.returncode:
                    raise ValueError("Файл отклонён антивирусом")
    except (
        ValueError,
        OSError,
        BadZipFile,
        ElementTree.ParseError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise ValidationError(
            "Документ отклонён: неверный формат, повреждённое или небезопасное содержимое."
        ) from exc
    finally:
        upload.seek(position)
