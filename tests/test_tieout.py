from decimal import Decimal
from pathlib import Path
import json

import fitz
import pytest

from src.tieout_validator import (
    BankRow,
    GatewayPayout,
    extract_pdf_lines,
    load_bank_csv,
    load_gateway_csv,
    main,
    reconcile_tieout,
)

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def test_synthetic_tieout_zero_variance():
    bank = load_bank_csv(FIX / "synthetic_bank_stmt.csv")
    payouts = load_gateway_csv(FIX / "synthetic_stripe_payouts.csv")
    res = reconcile_tieout(bank, payouts)
    assert res.passed is True
    assert res.variance == Decimal("0.00")
    assert res.matched_payouts == 2
    assert res.unmatched_gateway_ids == []
    assert res.duplicate_gateway_ids == []


def test_broken_checksum_rejected():
    with pytest.raises(ValueError, match="Checksum mismatch"):
        BankRow(
            date="2026-10-01",
            reference="BAD-1",
            description="Broken",
            opening_balance=Decimal("100.00"),
            amount=Decimal("50.00"),
            closing_balance=Decimal("140.00"),
        )


def test_broken_running_balance_chain_rejected(tmp_path: Path):
    bad_csv = tmp_path / "broken_chain.csv"
    bad_csv.write_text(
        "date,reference,description,opening_balance,amount,closing_balance\n"
        "2026-10-01,DEP-1,OPENING,100.00,50.00,150.00\n"
        "2026-10-02,DEP-2,CHAIN BREAK,155.00,25.00,180.00\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Chain break"):
        load_bank_csv(bad_csv)


def test_gateway_fee_math_mismatch_rejected():
    with pytest.raises(ValueError, match="Gateway fee math mismatch"):
        GatewayPayout(
            payout_id="STR-BAD",
            settled_date="2026-10-01",
            gross_amount=Decimal("1000.00"),
            processor_fee=Decimal("30.00"),
            net_payout=Decimal("960.00"),
        )


def test_missing_payout_flags_variance():
    bank = load_bank_csv(FIX / "synthetic_bank_stmt.csv")
    payouts = load_gateway_csv(FIX / "synthetic_stripe_payouts.csv") + [
        GatewayPayout(
            payout_id="STR-999",
            settled_date="2026-10-15",
            gross_amount=Decimal("200.00"),
            processor_fee=Decimal("6.00"),
            net_payout=Decimal("194.00"),
        )
    ]
    res = reconcile_tieout(bank, payouts)
    assert res.passed is False
    assert res.variance == Decimal("194.00")
    assert res.unmatched_gateway_ids == ["STR-999"]


def test_duplicate_bank_credit_flagged(tmp_path: Path):
    dup_csv = tmp_path / "dup_bank.csv"
    dup_csv.write_text(
        "date,reference,description,opening_balance,amount,closing_balance\n"
        "2026-10-01,DEP-1,STRIPE PAYOUT #STR-101,1000.00,4850.00,5850.00\n"
        "2026-10-02,DEP-2,STRIPE PAYOUT #STR-101,5850.00,4850.00,10700.00\n"
        "2026-10-05,DEP-3,STRIPE PAYOUT #STR-102,10700.00,3120.50,13820.50\n",
        encoding="utf-8",
    )
    res = reconcile_tieout(
        load_bank_csv(dup_csv),
        load_gateway_csv(FIX / "synthetic_stripe_payouts.csv"),
    )
    assert res.passed is False
    assert res.duplicate_gateway_ids == ["STR-101"]
    assert res.variance == Decimal("-4850.00")


def test_extract_pdf_lines_reads_synthetic_pdf(tmp_path: Path):
    pdf_path = tmp_path / "synthetic_stmt.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "2026-10-01 DEP-901 STRIPE PAYOUT #STR-101 4850.00")
    doc.save(str(pdf_path))
    doc.close()

    lines = extract_pdf_lines(pdf_path)
    assert any("STR-101" in line for line in lines)


def test_cli_exit_codes_and_json_output(tmp_path: Path):
    out_file = tmp_path / "tieout.json"
    exit_ok = main(
        [
            "--bank",
            str(FIX / "synthetic_bank_stmt.csv"),
            "--gateway",
            str(FIX / "synthetic_stripe_payouts.csv"),
            "--output",
            str(out_file),
        ]
    )
    assert exit_ok == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["variance"] == "0.00"

    bad_gateway = tmp_path / "bad_gateway.csv"
    bad_gateway.write_text(
        "payout_id,settled_date,gross_amount,processor_fee,net_payout\n"
        "STR-404,2026-10-10,500.00,15.00,485.00\n",
        encoding="utf-8",
    )
    exit_fail = main(
        [
            "--bank",
            str(FIX / "synthetic_bank_stmt.csv"),
            "--gateway",
            str(bad_gateway),
        ]
    )
    assert exit_fail == 1
