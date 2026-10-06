"""Read the two closure-rule answers from an attached InsuBiz PDF report.

Only text-bearing reports with recognizable vector checkboxes are supported.
Unknown marks and layouts fail explicitly; an absent mark is never guessed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import pymupdf

from insubiz import InsuBizClient, InsuBizError


logger = logging.getLogger(__name__)
REPORT_TITLE = "Skema for krænkende handlinger"


class PdfReportError(InsuBizError):
    """A report cannot be read or linked unambiguously to the case."""


@dataclass(frozen=True)
class PdfReport:
    incident_number: int
    score: int
    crisis_help: bool

    def as_infringing_act(self) -> dict:
        return {"postActQ1": self.crisis_help,
                **{f"reactionQ{n}": n == self.score for n in range(1, 11)}}


def _one_anchor(page: pymupdf.Page, text: str) -> pymupdf.Rect:
    matches = page.search_for(text)
    if len(matches) != 1:
        raise PdfReportError("PDF: nødvendigt tekstfelt mangler eller findes flere gange")
    return matches[0]


def _rectangle(drawing: dict) -> bool:
    """Distinguish rectangle outlines from the bounds of a checkmark."""
    items = drawing["items"]
    if len(items) == 1 and items[0][0] == "re":
        return True
    if len(items) != 4 or any(item[0] != "l" for item in items):
        return False
    points = set()
    for _, start, end in items:
        if abs(start.x - end.x) > .1 and abs(start.y - end.y) > .1:
            return False
        points.update(((round(start.x, 1), round(start.y, 1)),
                       (round(end.x, 1), round(end.y, 1))))
    return len(points) == 4


def _checkboxes(drawings: list[dict]) -> list[pymupdf.Rect]:
    rectangles = []
    for drawing in drawings:
        rect = drawing["rect"]
        ink = drawing.get("color") or drawing.get("fill")
        if (6 <= rect.width <= 16 and abs(rect.width - rect.height) < .6
                and ink is not None and min(ink) < .8 and _rectangle(drawing)):
            rectangles.append(rect)
    # Word-exported reports draw the border as a filled square with a white
    # square over it. Keep only the outside rectangle, once per checkbox.
    result = []
    for rect in sorted(rectangles, key=lambda r: -r.width):
        if not any(existing.contains(rect) for existing in result):
            result.append(rect)
    return result


def _printed_tick(drawing: dict, box: pymupdf.Rect) -> bool:
    """Recognize the six-vertex filled tick used in InsuBiz's report template."""
    rect = drawing["rect"]
    items = drawing["items"]
    fill = drawing.get("fill")
    if (fill is None or max(fill) > .2 or len(items) != 6
            or any(item[0] != "l" for item in items)
            or rect.width < box.width * .35 or rect.width > box.width * .8
            or abs(rect.width - rect.height) > box.width * .1
            or not box.contains(rect)):
        return False
    # Check the actual polygon, not just the presence of dark pixels: a blob,
    # question mark, crossed-out answer or annotation must not become a tick.
    if any(abs(items[n][2].x - items[(n + 1) % 6][1].x) > .1
           or abs(items[n][2].y - items[(n + 1) % 6][1].y) > .1 for n in range(6)):
        return False
    points = [((item[1].x - rect.x0) / rect.width,
               (item[1].y - rect.y0) / rect.height) for item in items]
    template = [(1, 0), (1, .4), (.4, 1), (0, .6), (0, .2), (.4, .6)]
    for order in (template, template[::-1]):
        for start in range(6):
            shifted = order[start:] + order[:start]
            if all(abs(x - tx) < .08 and abs(y - ty) < .08
                   for (x, y), (tx, ty) in zip(points, shifted)):
                return True
    return False


def _checked(page: pymupdf.Page, drawings: list[dict], box: pymupdf.Rect) -> bool:
    margin = box.width * .1
    interior = box + (margin, margin, -margin, -margin)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(6, 6), clip=interior,
                            colorspace=pymupdf.csGRAY, alpha=False)
    samples = pixmap.samples
    dark_fraction = sum(value < 180 for value in samples) / len(samples)
    ticks = [drawing for drawing in drawings if _printed_tick(drawing, box)]
    if dark_fraction < .005 and not ticks:
        return False
    if len(ticks) == 1 and .15 <= dark_fraction <= .4:
        # Verify that all visible ink belongs to the recognized mark.
        tick_rect = ticks[0]["rect"] + (-.4, -.4, .4, .4)
        for y in range(pixmap.height):
            for x in range(pixmap.width):
                if samples[y * pixmap.width + x] < 180:
                    point = pymupdf.Point((pixmap.x + x + .5) / 6,
                                          (pixmap.y + y + .5) / 6)
                    if not tick_rect.contains(point):
                        raise PdfReportError("PDF: afkrydsning har ekstra eller utydelige markeringer")
        return True
    raise PdfReportError("PDF: afkrydsning kunne ikke aflæses entydigt")


def parse_pdf_report(content: bytes, incident_number: int,
                     system_owner_id: int | None = None) -> PdfReport | None:
    """Return None for an unrelated text PDF; reject ambiguous reports."""
    if type(incident_number) is not int or incident_number <= 0:
        raise PdfReportError("PDF: sagens skadenummer mangler")
    try:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            if document.needs_pass or not 1 <= len(document) <= 100:
                raise PdfReportError("PDF: dokumentet er låst, tomt eller har for mange sider")
            pages = [page for page in document if page.search_for(REPORT_TITLE)]
            if not pages:
                if any(not page.get_text().strip() for page in document):
                    raise PdfReportError("PDF: billedbaserede sider kræver manuel vurdering")
                return None
            if len(pages) != 1:
                raise PdfReportError("PDF: dokumentet indeholder flere krænkelsesskemaer")
            page = pages[0]
            title = _one_anchor(page, REPORT_TITLE)
            case_label = _one_anchor(page, "Skade nr.")
            words = page.get_text("words")
            numbers = [int(word[4]) for word in words if word[4].isdigit()
                       and case_label.x1 < word[0] < case_label.x1 + 100
                       and abs((word[1] + word[3]) / 2 - (case_label.y0 + case_label.y1) / 2) < 3]
            if numbers != [incident_number]:
                raise PdfReportError("PDF: rapportens skadenummer matcher ikke sagen entydigt")
            if system_owner_id is not None:
                header = page.get_text(clip=pymupdf.Rect(0, title.y1, page.rect.width, case_label.y0))
                owners = [int(value) for value in re.findall(r"\((\d+)\)", header)]
                if owners != [system_owner_id]:
                    raise PdfReportError("PDF: rapportens systemejer matcher ikke konfigurationen")
            heading = _one_anchor(page, "Hvordan var din umiddelbare reaktion")
            crisis = _one_anchor(page, "Psykologisk krisehjælp")
            drawings = page.get_drawings()
            boxes = _checkboxes(drawings)
            scale_boxes = sorted([box for box in boxes
                                  if heading.y1 < box.y0 < heading.y1 + 30], key=lambda box: box.x0)
            if len(scale_boxes) != 10:
                raise PdfReportError("PDF: reaktionsskalaen indeholder ikke ti genkendelige felter")
            scores = []
            for score, box in enumerate(scale_boxes, 1):
                labels = [word[4] for word in words if word[4].isdigit()
                          and 0 <= box.x0 - word[2] <= box.width
                          and box.y0 - 3 < (word[1] + word[3]) / 2 < box.y1 + 3]
                if labels != [str(score)]:
                    raise PdfReportError("PDF: skalaens tal og afkrydsningsfelter kan ikke kobles entydigt")
                if _checked(page, drawings, box):
                    scores.append(score)
            if len(scores) != 1:
                raise PdfReportError("PDF: reaktionsskalaen mangler eller har flere svar")
            crisis_boxes = [box for box in boxes
                            if 0 <= crisis.x0 - box.x1 <= box.width * 1.5
                            and box.y0 - 2 < (crisis.y0 + crisis.y1) / 2 < box.y1 + 2]
            if len(crisis_boxes) != 1:
                raise PdfReportError("PDF: krisehjælpsfeltet kan ikke findes entydigt")
            return PdfReport(incident_number, scores[0], _checked(page, drawings, crisis_boxes[0]))
    except PdfReportError:
        raise
    except Exception:
        # Library errors can contain document text; keep it out of audit logs.
        raise PdfReportError("PDF: dokumentet kunne ikke læses") from None


async def read_incident_pdf_report(
    client: InsuBizClient, incident: dict, *, documents: list[dict] | None = None,
    required_document_id: int | None = None,
) -> tuple[PdfReport, int] | None:
    """Require exactly one readable matching report among the case's PDFs."""
    if documents is None:
        documents = await client.get_incident_documents(incident["id"])
    if not isinstance(documents, list) or any(not isinstance(doc, dict) for doc in documents):
        raise PdfReportError("PDF: dokumentlisten har ugyldig struktur")
    reports = {}
    for metadata in documents:
        file_type = str(metadata.get("fileType") or "").lower().lstrip(".")
        if file_type not in {"pdf", "application/pdf"}:
            continue
        document_id = metadata.get("id")
        if type(document_id) is not int or document_id <= 0:
            raise PdfReportError("PDF: dokumentlisten indeholder ugyldigt dokument-id")
        if document_id in reports:
            continue
        content = await client.download_incident_document(document_id)
        # PyMuPDF does not support use from multiple threads. Keep PDF work on
        # the workflow's thread; only the client's network download is offloaded.
        report = parse_pdf_report(content, incident.get("incidentNumberInternal"),
                                  client.system_owner_id)
        logger.info("Sag %s: PDF-dokument %s %s", incident["id"], document_id,
                    "indeholder et genkendeligt krænkelsesskema" if report else "indeholder ikke krænkelsesskemaet")
        if report:
            reports[document_id] = report
    if len(reports) > 1:
        raise PdfReportError("PDF: flere krænkelsesrapporter på sagen; kræver manuel vurdering")
    if required_document_id is not None and list(reports) != [required_document_id]:
        raise PdfReportError("PDF: køelementets rapport findes ikke længere entydigt på sagen")
    if not reports:
        return None
    document_id, report = next(iter(reports.items()))
    return report, document_id
