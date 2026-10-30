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
        # natural language: the keyword is enough, no fixed phrase is needed
        "resumen de este mes por favor",
        "mis gastos",
        "mostrame mis gastos",
        "gastos del mes",
        "¿cuánto gasté?",
        "Cuánto gasté este mes?",
        "¿cuánta plata gasté en total?",
        "cuánto llevo gastado",
        "Hola, ¿me podés pasar un resumen de todo lo que gasté este mes?",
        "quiero ver mi reporte de los últimos 30 días",
        "Cuánto gasté en 2026?",
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
        # out-of-domain chatter has no report keyword
        "Qué es un mvp?",
        "Hola bot",
        # a bare number is an amount too, whatever the keyword next to it
        "anotá mis gastos de hoy: café 500",
        "compré mis gastos comunes 300",
        "gasté en el súper",
        # correcting a record is not asking for a report
        "me equivoqué en mis gastos de ayer",
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


def test_numbers_that_name_a_period_do_not_make_a_request_an_expense():
    assert is_report_request("resumen de octubre 2026") is True
    assert is_report_request("mis gastos de los últimos 30 días") is True
    assert is_report_request("mis gastos de 500") is False
