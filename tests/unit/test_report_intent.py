"""Unit tests for report-request detection: requests are recognized, real expenses never are."""

import pytest
from src.services.report_intent import is_report_request


@pytest.mark.parametrize(
    "text",
    [
        "Quiero mi resumen",
        "quiero mi resumen de gastos",
        "Dame un reporte",
        "pasame el resumen por favor",
        "mi resumen",
        "Resumen",
        "reporte",
        "enviame mi informe",
        "necesito el extracto de mis gastos",
        "¿Me mandás el resumen?",
        "reporte de gastos del mes",
        "Resumen de mis gastos de octubre 2026",
        "podés armarme un reporte?",
        "QUIERO MI RESUMEN!!!",
        "mandame el reporte",
        "Quiero mi resumén",
    ],
)
def test_report_requests_are_detected(text):
    assert is_report_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Gasté 500 en el resumen del consorcio",
        "pagué el resumen de la tarjeta",
        "me mandaron el resumen de la tarjeta, 45.000 pesos",
        "Almorcé sushi por 15.50 USD pagando con débito",
        "Ayer pagué 3500 pesos en la farmacia San Pablo",
        "uber 1.200",
        "gasté 15 usd",
        "hola",
        "Pagué la luz",
        "compré un libro de resumen de matemática ayer en la librería del centro por favor",
        "",
        "   ",
    ],
)
def test_expenses_and_chatter_are_never_taken_for_report_requests(text):
    assert is_report_request(text) is False


def test_a_request_with_an_amount_is_an_expense():
    """An expense always has an amount: that is what separates 'resumen' as a request from 'resumen' as a purchase."""
    assert is_report_request("quiero mi resumen $5000") is False
    assert is_report_request("dame el reporte 2.500 pesos") is False


def test_non_text_input_is_not_a_request():
    assert is_report_request(None) is False
