"""Customer documents use a single Decimal calculation and never internal costs."""

from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .services import economics, rounded


def customer_rows(quote):
    e = economics(quote)
    rows = []
    for r in e["rows"]:
        # All three representations expose the same net/tax/gross totals.
        rows.append(
            [
                r["name"] + f" (НДС {r['rate']}%)",
                r["store_code"],
                r["quantity"],
                r["unit"],
                rounded(r["price"] * (1 + r["rate"] / 100))
                if quote.vat_mode == "included"
                else r["price"],
                r["net"],
                r["vat"],
                r["gross"],
            ]
        )
    return e, rows


def render_quote(quote, format):
    e, rows = customer_rows(quote)
    status = "СОГЛАСОВАНО" if quote.status == "approved" else "НЕ СОГЛАСОВАНО"
    title = f"OVI · Коммерческое предложение № {quote.pk}"
    subtitle = f"{quote.project.company.name} · " + (
        "Продажа с монтажом" if quote.installation else "Продажа без монтажа"
    )
    mode = {
        "included": "НДС включён в итоговую стоимость",
        "excluded": "Цены без НДС; НДС начисляется по ставке каждой позиции",
        "separate": "НДС выделен отдельной строкой",
    }[quote.vat_mode]
    headers = [
        "Наименование",
        "Код магазина",
        "Кол-во",
        "Ед.",
        "Цена с НДС" if quote.vat_mode == "included" else "Цена без НДС",
        "Без НДС",
        "НДС",
        "С НДС",
    ]
    totals = [("Без НДС", e["net"]), ("НДС", e["vat"]), ("Итого с НДС", e["gross"])]
    output = BytesIO()
    if format == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "КП"
        for idx, value in enumerate(
            [
                title,
                subtitle,
                status,
                mode,
                f"Действительно до {quote.valid_until:%d.%m.%Y}",
            ],
            1,
        ):
            ws.merge_cells(start_row=idx, start_column=1, end_row=idx, end_column=8)
            cell = ws.cell(idx, 1, value)
            cell.data_type = "s"
            cell.font = Font(
                name="Calibri",
                size=16 if idx == 1 else 11,
                bold=idx in (1, 3),
                color="9C1C27" if idx == 3 and quote.status != "approved" else "173F35",
            )
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            ws.row_dimensions[idx].height = 32
        ws.append(headers)
        for row in rows:
            ws.append(row)
            for cell in ws[ws.max_row]:
                if isinstance(cell.value, str):
                    cell.data_type = "s"  # No Excel formula injection from names.
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if cell.column >= 5:
                    cell.number_format = "#,##0.00"
            ws.row_dimensions[ws.max_row].height = 42
        for label, amount in totals:
            ws.append(["", "", "", "", "", "", label, amount])
            ws.cell(ws.max_row, 8).number_format = "#,##0.00"
            ws.cell(ws.max_row, 8).font = Font(bold=True)
        for cell in ws[6]:
            cell.fill = PatternFill("solid", fgColor="173F35")
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(wrap_text=True)
        for col, width in zip("ABCDEFGH", [40, 15, 12, 10, 19, 19, 19, 19]):
            ws.column_dimensions[col].width = width
        ws.row_dimensions[6].height = 32
        ws.freeze_panes = "A7"
        ws.print_title_rows = "1:6"
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        wb.save(output)
        return (
            output.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    font_path = Path(settings.BASE_DIR) / "static/fonts/DejaVuSans.ttf"
    if "OVI" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("OVI", str(font_path)))
    style = ParagraphStyle("body", fontName="OVI", fontSize=8, leading=12)
    heading = ParagraphStyle(
        "heading",
        parent=style,
        fontSize=18,
        leading=24,
        textColor=colors.HexColor("#173F35"),
    )

    def p(value):
        return Paragraph(escape(str(value)), style)

    body = [
        Paragraph(escape(title), heading),
        Spacer(1, 12),
        p(subtitle),
        p(status),
        p(mode),
        p(f"Действительно до {quote.valid_until:%d.%m.%Y}"),
        Spacer(1, 16),
    ]
    table_rows = [[p(h) for h in headers]]
    for row in rows:
        table_rows.append(
            [p(f"{v:,.2f}" if hasattr(v, "quantize") else v) for v in row]
        )
    for label, amount in totals:
        table_rows.append([p("")] * 6 + [p(label), p(f"{amount:,.2f}")])
    table = Table(table_rows, colWidths=[190, 55, 45, 35, 80, 90, 85, 90], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5EFEA")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.HexColor("#173F35")),
                ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#CBD8D1")),
            ]
        )
    )
    body.append(table)

    def footer(canvas, document):
        canvas.setFont("OVI", 8)
        canvas.drawString(36, 20, f"OVI · КП № {quote.pk} · {status}")
        canvas.drawRightString(805, 20, str(document.page))

    SimpleDocTemplate(
        output,
        pagesize=(842, 595),
        leftMargin=36,
        rightMargin=36,
        topMargin=32,
        bottomMargin=36,
    ).build(body, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue(), "application/pdf"
