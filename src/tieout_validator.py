"""Bank & Payment-Gateway Tie-Out Validator using Pydantic invariants and PyMuPDF."""
from __future__ import annotations

import argparse
import csv
import re
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class BankRow(BaseModel):
    """Single bank statement line with per-row running balance verification."""

    date: str
    reference: str
    description: str
    opening_balance: Decimal
    amount: Decimal
    closing_balance: Decimal

    @model_validator(mode="after")
    def check_running_balance(self) -> "BankRow":
        expected = (self.opening_balance + self.amount).quantize(Decimal("0.01"))
        actual = self.closing_balance.quantize(Decimal("0.01"))
        if expected != actual:
            raise ValueError(f"Checksum mismatch on {self.reference}: {expected} != {actual}")
        return self


class GatewayPayout(BaseModel):
    """Payment-gateway settlement batch enforcing gross - fee == net payout."""

    payout_id: str
    settled_date: str
    gross_amount: Decimal = Field(ge=0)
    processor_fee: Decimal = Field(ge=0)
    net_payout: Decimal

    @model_validator(mode="after")
    def check_net_tieout(self) -> "GatewayPayout":
        expected = (self.gross_amount - self.processor_fee).quantize(Decimal("0.01"))
        actual = self.net_payout.quantize(Decimal("0.01"))
        if expected != actual:
            raise ValueError(f"Gateway fee math mismatch on {self.payout_id}: {expected} != {actual}")
        return self


class TieOutSummary(BaseModel):
    """Aggregate reconciliation verdict across bank credits and gateway payouts."""

    bank_rows: int
    gateway_payouts: int
    matched_payouts: int
    unmatched_gateway_ids: List[str]
    duplicate_gateway_ids: List[str] = Field(default_factory=list)
    total_gateway_gross: Decimal
    total_gateway_fees: Decimal
    total_gateway_net: Decimal
    total_bank_gateway_credits: Decimal
    variance: Decimal
    passed: bool


def extract_pdf_lines(pdf_path: Path) -> List[str]:
    """Extract non-empty text lines from a PDF statement via pymupdf4llm or PyMuPDF."""
    try:
        import pymupdf4llm  # type: ignore

        md = pymupdf4llm.to_markdown(str(pdf_path))
        return [ln.strip() for ln in md.splitlines() if ln.strip()]
    except ImportError:
        import fitz  # type: ignore

        with fitz.open(str(pdf_path)) as doc:
            lines: List[str] = []
            for page in doc:
                lines.extend(ln.strip() for ln in page.get_text("text").splitlines() if ln.strip())
            return lines


def load_bank_csv(path: Path) -> List[BankRow]:
    """Load bank statement CSV and enforce continuous opening/closing balance chaining."""
    rows: List[BankRow] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for raw in csv.DictReader(fh):
            rows.append(BankRow(**raw))
    if not rows:
        raise ValueError(f"Bank CSV contains no rows: {path}")
    for i in range(1, len(rows)):
        prev_close = rows[i - 1].closing_balance.quantize(Decimal("0.01"))
        curr_open = rows[i].opening_balance.quantize(Decimal("0.01"))
        if curr_open != prev_close:
            raise ValueError(
                f"Chain break between {rows[i-1].reference} ({prev_close}) "
                f"and {rows[i].reference} ({curr_open})"
            )
    return rows


def load_gateway_csv(path: Path) -> List[GatewayPayout]:
    """Load payment gateway payout CSV and validate fee arithmetic on every row."""
    with path.open("r", encoding="utf-8", newline="") as fh:
        payouts = [GatewayPayout(**raw) for raw in csv.DictReader(fh)]
    if not payouts:
        raise ValueError(f"Gateway CSV contains no rows: {path}")
    return payouts


def reconcile_tieout(bank_rows: List[BankRow], payouts: List[GatewayPayout]) -> TieOutSummary:
    """Match each gateway payout ID against bank credits and verify $0.00 net variance."""
    match_counts: Dict[str, int] = {p.payout_id: 0 for p in payouts}
    bank_gateway_total = Decimal("0.00")

    for row in bank_rows:
        for p in payouts:
            pattern = rf"(?<![A-Za-z0-9]){re.escape(p.payout_id)}(?![A-Za-z0-9])"
            if re.search(pattern, row.description, re.IGNORECASE) and (
                row.amount.quantize(Decimal("0.01")) == p.net_payout.quantize(Decimal("0.01"))
            ):
                match_counts[p.payout_id] += 1
                bank_gateway_total += row.amount.quantize(Decimal("0.01"))

    total_gross = sum((p.gross_amount for p in payouts), Decimal("0.00")).quantize(Decimal("0.01"))
    total_fees = sum((p.processor_fee for p in payouts), Decimal("0.00")).quantize(Decimal("0.01"))
    total_net = sum((p.net_payout for p in payouts), Decimal("0.00")).quantize(Decimal("0.01"))
    matched_count = sum(1 for count in match_counts.values() if count >= 1)
    unmatched = [pid for pid, count in match_counts.items() if count == 0]
    duplicates = [pid for pid, count in match_counts.items() if count > 1]
    variance = (total_net - bank_gateway_total).quantize(Decimal("0.01"))

    return TieOutSummary(
        bank_rows=len(bank_rows),
        gateway_payouts=len(payouts),
        matched_payouts=matched_count,
        unmatched_gateway_ids=unmatched,
        duplicate_gateway_ids=duplicates,
        total_gateway_gross=total_gross,
        total_gateway_fees=total_fees,
        total_gateway_net=total_net,
        total_bank_gateway_credits=bank_gateway_total.quantize(Decimal("0.01")),
        variance=variance,
        passed=(variance == Decimal("0.00") and len(unmatched) == 0 and len(duplicates) == 0),
    )


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint printing JSON summary and returning 0 on exact tie-out."""
    ap = argparse.ArgumentParser(description="Bank & Payment-Gateway Tie-Out Validator")
    ap.add_argument("--bank", type=Path, required=True, help="Path to bank statement CSV")
    ap.add_argument("--gateway", type=Path, required=True, help="Path to gateway payouts CSV")
    ap.add_argument("--output", type=Path, default=None, help="Optional output path for JSON summary")
    args = ap.parse_args(argv)
    summary = reconcile_tieout(load_bank_csv(args.bank), load_gateway_csv(args.gateway))
    payload = summary.model_dump_json(indent=2)
    if args.output is not None:
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
