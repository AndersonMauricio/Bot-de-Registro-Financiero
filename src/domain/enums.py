"""Domain enums for standardized taxonomies."""

import re
import unicodedata
from enum import Enum


def _normalize(value) -> str:
    """Lowercase, strip accents and collapse spaces/underscores/hyphens."""
    ascii_text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[\s_\-]+", " ", ascii_text).strip().casefold()


class SpanishTaxonomyEnum(str, Enum):
    """Base for taxonomies stored in Spanish: lookups ignore case, accents and underscores, but unknown values still fail."""

    @classmethod
    def _missing_(cls, value):
        wanted = _normalize(value)
        for member in cls:
            if wanted in (_normalize(member.value), _normalize(member.name)):
                return member
        return None


class CategoryEnum(SpanishTaxonomyEnum):
    """Standardized macro categories for expenses (strictly Spanish)."""
    ALIMENTACION = "Alimentación"
    TRANSPORTE = "Transporte"
    VIVIENDA = "Vivienda"
    SERVICIOS = "Servicios"
    SALUD = "Salud"
    ENTRETENIMIENTO = "Entretenimiento"
    EDUCACION = "Educación"
    COMPRAS = "Compras"
    OTROS = "Otros"


class PaymentMethodEnum(SpanishTaxonomyEnum):
    """Normalized payment methods (strictly Spanish)."""
    EFECTIVO = "Efectivo"
    TARJETA_CREDITO = "Tarjeta de Crédito"
    TARJETA_DEBITO = "Tarjeta de Débito"
    TRANSFERENCIA = "Transferencia"
    BILLETERA_VIRTUAL = "Billetera Virtual"
    NO_ESPECIFICADO = "No Especificado"


class InputTypeEnum(str, Enum):
    """Input media channel from WhatsApp."""
    TEXT = "TEXT"
    AUDIO = "AUDIO"
    IMAGE = "IMAGE"


class UserStatusEnum(int, Enum):
    """Access state stored in the users sheet. An empty cell (unknown / pending) is represented by None."""
    APPROVED = 1
    REJECTED = 2
