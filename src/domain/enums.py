"""Domain enums for standardized taxonomies."""

from enum import Enum


class CategoryEnum(str, Enum):
    """Standardized macro categories for expenses."""
    ALIMENTACION = "ALIMENTACION"
    TRANSPORTE = "TRANSPORTE"
    VIVIENDA = "VIVIENDA"
    SERVICIOS = "SERVICIOS"
    SALUD = "SALUD"
    ENTRETENIMIENTO = "ENTRETENIMIENTO"
    EDUCACION = "EDUCACION"
    COMPRAS = "COMPRAS"
    OTROS = "OTROS"


class PaymentMethodEnum(str, Enum):
    """Normalized payment methods."""
    CASH = "CASH"
    CREDIT_CARD = "CREDIT_CARD"
    DEBIT_CARD = "DEBIT_CARD"
    BANK_TRANSFER = "BANK_TRANSFER"
    CRYPTO = "CRYPTO"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class InputTypeEnum(str, Enum):
    """Input media channel from WhatsApp."""
    TEXT = "TEXT"
    AUDIO = "AUDIO"
    IMAGE = "IMAGE"


class UserStatusEnum(int, Enum):
    """Access state stored in the users sheet. An empty cell (unknown / pending) is represented by None."""
    APPROVED = 1
    REJECTED = 2
