import pytest

from apps.api.ingestion import parse_csv_rows, verify_webhook_signature


def test_csv_validation_rejects_formula_values():
    with pytest.raises(ValueError, match="formula"):
        parse_csv_rows(
            "tenant_id,traveler_id,segment_id,carrier_code,flight_number\nacme,=x,s1,UA,1\n"
        )


def test_webhook_signature_verification():
    assert (
        verify_webhook_signature(
            body=b'{"event":"trip.updated"}',
            signature="f5b985c3e2a76f918aa596e89310fefcf9b8e2e69698e9d5ba6b43a3fd48715c",
            secret="secret",
        )
        is False
    )
