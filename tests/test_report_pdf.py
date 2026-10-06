"""Synthetic reports only: no real names, reports or server access in tests."""

import asyncio
import logging
from io import BytesIO
from unittest.mock import Mock, patch

import pymupdf
import pytest

from diagnostics import diagnose_incidents
from insubiz import InsuBizClient, InsuBizError, evaluate_eligibility
from main import populate_queue, process_workqueue
from report_pdf import PdfReportError, parse_pdf_report, read_incident_pdf_report


def synthetic_pdf(scores=(7,), crisis=False, number=9560, owner=141,
                  unknown=False, missing_crisis=False, missing_box=False,
                  unrelated=False, scale=1, offset=0, extras=False):
    document = pymupdf.open()
    page = document.new_page()
    if unrelated:
        page.insert_text((50, 50), "An unrelated letter")
        return document.tobytes()

    def point(x, y):
        return pymupdf.Point(x * scale + offset, y * scale + offset)

    def text(x, y, value, size=10):
        page.insert_text(point(x, y), value, fontsize=size * scale)

    def box(x, y, checked=False, blob=False):
        rect = pymupdf.Rect(point(x, y), point(x + 10, y + 10))
        page.draw_rect(rect, color=None, fill=(.5, .5, .5))
        inset = .75 * scale
        page.draw_rect(rect + (inset, inset, -inset, -inset), color=None, fill=(1, 1, 1))
        if checked:
            # The printed InsuBiz tick's polygon; unlike an OCR fixture, this
            # exercises the actual geometry and raster checks together.
            vertices = [(1, 0), (1, .4), (.4, 1), (0, .6), (0, .2), (.4, .6)]
            path = [point(x + 2.2 + a * 5.5, y + 2.2 + b * 5.5) for a, b in vertices]
            shape = page.new_shape()
            shape.draw_polyline(path)
            shape.finish(color=None, fill=(0, 0, 0), closePath=True)
            shape.commit()
        if blob:
            page.draw_rect(pymupdf.Rect(point(x + 3, y + 3), point(x + 7, y + 7)),
                           color=None, fill=(0, 0, 0))
        if checked and extras:
            page.draw_line(point(x + 1.2, y + 2), point(x + 1.2, y + 8), width=.4)

    text(50, 60, "Skema for krænkende handlinger", 18)
    text(50, 80, f"Testkommune ({owner})")
    text(50, 100, "Skade nr.:")
    text(130, 100, str(number))
    text(50, 180, "Hvordan var din umiddelbare reaktion på hændelsen?")
    for n in range(1, 11):
        x = 50 + (n - 1) * 25
        text(x, 202, str(n), 8)
        if not (missing_box and n == 10):
            box(x + 10, 194, n in scores and not unknown, unknown and n in scores)
    if not missing_crisis:
        text(70, 300, "Psykologisk krisehjælp")
        box(50, 289, crisis)
    text(50, 350, "HEMMELIG_DOKUMENTTEKST")
    return document.tobytes()


def case(status=0):
    return {"id": 12, "incidentNumberInternal": 9560,
            "customer": {"id": 100}, "status": {"id": status},
            "personalInjury": {"accidentDuration": {"id": 1}}}


def client(content=None, status=0):
    result = Mock(spec=InsuBizClient)
    result.system_owner_id = 141
    result.get_incident.return_value = case(status)
    result.find_incidents_by_status.return_value = {"data": [{"id": 12}], "totalRows": 1}
    result.get_infringing_acts_by_status.return_value = {"data": [], "totalRows": 0}
    result.get_customer_infringing_acts.return_value = {"data": [], "totalRows": 0}
    result.get_incident_documents.return_value = [{"id": 77, "fileType": ".pdf"}]
    result.download_incident_document.return_value = content or synthetic_pdf()
    return result


class PdfWorkItem:
    id = 7
    data = {"incident_id": 12, "source": "pdf_report", "report_document_id": 77}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("scale,offset", [(1, 0), (.85, 15), (1.2, 20)])
@pytest.mark.parametrize("score", range(1, 11))
def test_all_scores_survive_page_size_and_position_changes(score, scale, offset):
    report = parse_pdf_report(synthetic_pdf((score,), scale=scale, offset=offset), 9560, 141)
    assert report.score == score
    assert report.crisis_help is False
    assert evaluate_eligibility(case(), report.as_infringing_act()).eligible == (score <= 7)


def test_checked_crisis_box_prevents_closure():
    report = parse_pdf_report(synthetic_pdf(crisis=True), 9560, 141)
    assert report.crisis_help is True
    assert not evaluate_eligibility(case(), report.as_infringing_act()).eligible


@pytest.mark.parametrize("options", [
    {"scores": ()}, {"scores": (2, 7)}, {"number": 1234}, {"owner": 999},
    {"unknown": True}, {"missing_crisis": True}, {"missing_box": True},
    {"extras": True},
])
def test_ambiguous_missing_or_unlinked_answers_are_rejected(options):
    with pytest.raises(PdfReportError):
        parse_pdf_report(synthetic_pdf(**options), 9560, 141)


def test_unrelated_letter_is_not_a_report():
    assert parse_pdf_report(synthetic_pdf(unrelated=True), 9560, 141) is None


def test_corrupt_pdf_and_image_only_pdf_are_not_interpreted_as_unchecked():
    with pytest.raises(PdfReportError):
        parse_pdf_report(b"not a pdf", 9560, 141)
    document = pymupdf.open()
    document.new_page()
    with pytest.raises(PdfReportError):
        parse_pdf_report(document.tobytes(), 9560, 141)


def test_report_can_be_on_second_page_but_two_reports_are_ambiguous():
    document = pymupdf.open(stream=synthetic_pdf(unrelated=True), filetype="pdf")
    report = pymupdf.open(stream=synthetic_pdf(), filetype="pdf")
    document.insert_pdf(report)
    assert parse_pdf_report(document.tobytes(), 9560, 141).score == 7
    document.insert_pdf(report)
    with pytest.raises(PdfReportError):
        parse_pdf_report(document.tobytes(), 9560, 141)


def test_fallback_queues_new_case_even_when_api_posts_are_empty(caplog):
    caplog.set_level(logging.INFO)
    api = client()
    queue = Mock()
    queue.get_item_by_reference.return_value = []
    assert asyncio.run(populate_queue(queue, api, 3)) == 1
    queue.add_item.assert_called_once_with(PdfWorkItem.data, reference="insubiz-incident-12")
    api.close_incident.assert_not_called()
    api.get_infringing_act.assert_not_called()
    assert "reaktionsscore=7" in caplog.text
    assert "krisehjælp=nej" in caplog.text
    assert "HEMMELIG" not in caplog.text


@pytest.mark.parametrize("options", [{"scores": (8,)}, {"crisis": True}, {"unknown": True}])
def test_fallback_never_queues_rejected_or_uncertain_answers(options):
    api = client(synthetic_pdf(**options))
    queue = Mock()
    assert asyncio.run(populate_queue(queue, api, 3)) == 0
    queue.add_item.assert_not_called()


@pytest.mark.parametrize("status", [1, 2, 3, None])
def test_fallback_cannot_process_case_with_other_status(status):
    api = client(status=status)
    assert asyncio.run(populate_queue(Mock(), api, 3)) == 0
    assert asyncio.run(process_workqueue([PdfWorkItem()], api, 3, False)) == 0
    api.download_incident_document.assert_not_called()
    api.close_incident.assert_not_called()


@pytest.mark.parametrize("dry_run", [True, False])
def test_pdf_queue_reloads_document_and_respects_dry_run(dry_run):
    api = client()
    assert asyncio.run(process_workqueue([PdfWorkItem()], api, 3, dry_run)) == 1
    api.get_incident_documents.assert_awaited_once_with(12)
    api.download_incident_document.assert_awaited_once_with(77)
    if dry_run:
        api.close_incident.assert_not_called()
    else:
        api.close_incident.assert_awaited_once_with(12, 3)


@pytest.mark.parametrize("options", [{"scores": (8,)}, {"crisis": True}, {"number": 1234},
                                    {"scores": (1, 2)}, {"unknown": True}])
def test_changed_document_never_closes_queued_case(options):
    api = client(synthetic_pdf(**options))
    assert asyncio.run(process_workqueue([PdfWorkItem()], api, 3, False)) == 0
    api.close_incident.assert_not_called()


@pytest.mark.parametrize("documents", [[], [{"id": 78, "fileType": "pdf"}],
                                     [{"id": 77, "fileType": "pdf"}, {"id": 78, "fileType": "pdf"}]])
def test_removed_replaced_or_multiple_reports_cannot_close_case(documents):
    api = client()
    api.get_incident_documents.return_value = documents
    assert asyncio.run(process_workqueue([PdfWorkItem()], api, 3, False)) == 0
    api.close_incident.assert_not_called()


def test_pdf_download_failure_leaves_case_unassessed_and_other_cases_continue(caplog):
    api = client()
    api.find_incidents_by_status.return_value = {"data": [{"id": 12}, {"id": 13}], "totalRows": 2}
    api.get_incident.side_effect = [case(), {**case(), "id": 13}]
    api.download_incident_document.side_effect = [InsuBizError("HTTP 500"), synthetic_pdf()]
    queue = Mock()
    queue.get_item_by_reference.return_value = []
    assert asyncio.run(populate_queue(queue, api, 3)) == 1
    assert queue.add_item.call_args.args[0]["incident_id"] == 13
    assert "Sag 12: ikke vurderet" in caplog.text


def test_diagnosis_reads_report_without_mutations_or_personal_logs(caplog):
    caplog.set_level(logging.INFO)
    api = client()
    asyncio.run(diagnose_incidents(api, [12]))
    api.close_incident.assert_not_called()
    assert "PDF-dokument=77" in caplog.text
    assert "reaktionsscore=7, krisehjælp=nej" in caplog.text
    assert "HEMMELIG" not in caplog.text


def test_download_returns_binary_pdf_with_bearer_authentication():
    api = InsuBizClient("https://example.test", "key", "secret")
    api.token = "test-token"
    with patch("insubiz.urlopen", return_value=BytesIO(synthetic_pdf())) as request:
        content = asyncio.run(api.download_incident_document(77))
    assert content.startswith(b"%PDF-")
    assert request.call_args.args[0].full_url == "https://example.test/api/v1.3/Incident/DownloadDocumentAsync?documentId=77"
    assert request.call_args.args[0].get_header("Authorization") == "Bearer test-token"


def test_download_rejects_json_error_response_even_with_http_200():
    api = InsuBizClient("https://example.test", "key", "secret")
    api.token = "test-token"
    with patch("insubiz.urlopen", return_value=BytesIO(b'{"error":"HEMMELIG"}')):
        with pytest.raises(InsuBizError, match="ikke en PDF"):
            asyncio.run(api.download_incident_document(77))


def test_unrelated_letter_does_not_hide_matching_report():
    api = client()
    api.get_incident_documents.return_value = [{"id": 76, "fileType": "pdf"}, {"id": 77, "fileType": "pdf"}]
    api.download_incident_document.side_effect = [synthetic_pdf(unrelated=True), synthetic_pdf()]
    report, document_id = asyncio.run(read_incident_pdf_report(api, case()))
    assert report.score == 7 and document_id == 77


@pytest.mark.parametrize("response", [{}, [], {"id": 13}, {"id": True}, {"id": "12"}])
def test_case_details_must_match_requested_api_id(response):
    from unittest.mock import AsyncMock

    api = InsuBizClient("https://example.test", "key", "secret")
    api._request = AsyncMock(return_value=response)
    with pytest.raises(InsuBizError, match="forventede API-sags-id"):
        asyncio.run(api.get_incident(12))
